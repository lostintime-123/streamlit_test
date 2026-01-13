import os
import streamlit as st
import pandas as pd
import json
from openai import OpenAI
import sqlite3
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
import threading
from contextlib import contextmanager
import re
import requests
from io import BytesIO

# 初始的品牌和型号映射（将作为备选）
INITIAL_BRAND_MODEL_MAPPING = {
    "广州数控": {
        "数控系统": ["GSK 980T", "GSK 928TA", "GSK 928TC"],
        "驱动器": ["DA98"]
    },
    "三菱": {
        "变频器": ["三菱FR-E700"]
    }
}

# 页面配置
st.set_page_config(
    page_title="数控设备故障诊断系统",
    page_icon="🔧",
    layout="wide"
)

# 创建线程本地存储
thread_local = threading.local()

def normalize_alarm_code(alarm_code):
    """规范化报警代码：去除开头多余的0，但保留小数点和小数部分"""
    if pd.isna(alarm_code) or alarm_code == '':
        return ''
    
    # 转换为字符串并去除空格
    alarm_str = str(alarm_code).strip()
    
    # 如果包含小数点，处理小数部分
    if '.' in alarm_str:
        # 分割整数部分和小数部分
        int_part, dec_part = alarm_str.split('.', 1)
        # 去除整数部分开头的0
        int_part = int_part.lstrip('0') or '0'
        # 重新组合
        return f"{int_part}.{dec_part}"
    else:
        # 去除开头的0，但如果全部是0则保留一个0
        return alarm_str.lstrip('0') or '0'

def extract_brand_model_mapping(df):
    """从数据框中提取品牌和型号的映射关系"""
    brand_model_mapping = {}
    
    # 检查数据框中是否有品牌和产品类型列
    has_brand_column = '品牌' in df.columns
    has_product_type_column = '产品类型' in df.columns
    has_model_column = '型号' in df.columns
    
    if has_brand_column and has_product_type_column and has_model_column:
        # 按品牌和产品类型分组，收集型号
        grouped = df.groupby(['品牌', '产品类型'])['型号'].unique()
        
        for (brand, product_type), models in grouped.items():
            if pd.notna(brand) and pd.notna(product_type) and brand != '未知' and product_type != '未知':
                if brand not in brand_model_mapping:
                    brand_model_mapping[brand] = {}
                
                # 过滤掉空值和未知值
                valid_models = [model for model in models if pd.notna(model) and model != '未知']
                if valid_models:
                    brand_model_mapping[brand][product_type] = valid_models
    
    return brand_model_mapping

class RobustDataLoader:
    """健壮的数据加载器，处理多子表Excel文件"""
    
    def __init__(self):
        self.df = None
        self.data_loaded = False
        self.error_message = None  # 添加错误信息属性
    
    def load_from_excel(self, file_obj):
        """从Excel文件加载数据"""
        try:
            # 重置加载状态
            self.data_loaded = False
            self.error_message = None  # 重置错误信息

            # 尝试确定文件类型并选择合适的引擎
            if hasattr(file_obj, 'name'):
                file_name = file_obj.name.lower()
                if file_name.endswith('.xlsx'):
                    engine = 'openpyxl'
                elif file_name.endswith('.xls'):
                    engine = 'xlrd'
                else:
                    # 默认使用openpyxl
                    engine = 'openpyxl'
            else:
                # 对于没有文件名的对象（如BytesIO），尝试两种引擎
                engine = None
            
            # 读取Excel文件
            if engine:
                excel_file = pd.ExcelFile(file_obj, engine=engine)
            else:
                # 尝试自动检测引擎
                try:
                    excel_file = pd.ExcelFile(file_obj, engine='openpyxl')
                except:
                    excel_file = pd.ExcelFile(file_obj, engine='xlrd')
            
            sheet_names = excel_file.sheet_names
            
            # 读取所有子表并合并
            all_sheets = []
            for sheet_name in sheet_names:
                try:
                    # 读取时不转换数据类型，保持原始格式
                    if engine:
                        sheet_df = pd.read_excel(file_obj, sheet_name=sheet_name, dtype=str, engine=engine)
                    else:
                        # 尝试自动检测引擎
                        try:
                            sheet_df = pd.read_excel(file_obj, sheet_name=sheet_name, dtype=str, engine='openpyxl')
                        except:
                            sheet_df = pd.read_excel(file_obj, sheet_name=sheet_name, dtype=str, engine='xlrd')
                    
                    # 检查是否为空表
                    if sheet_df.empty:
                        st.warning(f"工作表 '{sheet_name}' 为空，已跳过")
                        continue
                        
                    # 添加子表名称作为新列
                    sheet_df['来源子表'] = sheet_name
                    
                    # 标准化列名（去除前后空格）
                    sheet_df.columns = sheet_df.columns.str.strip()
                    
                    all_sheets.append(sheet_df)
                except Exception as e:
                    st.warning(f"读取工作表 '{sheet_name}' 时出错: {e}，已跳过")
            
            if not all_sheets:
                self.error_message = "没有成功读取任何工作表，请检查Excel文件格式"
                st.error(self.error_message)
                return False
            
            # 合并所有子表
            # 找出所有可能的列
            all_columns = set()
            for sheet in all_sheets:
                all_columns.update(sheet.columns)
            
            # 确保所有DataFrame有相同的列
            standardized_sheets = []
            for sheet in all_sheets:
                # 添加缺失的列
                for col in all_columns:
                    if col not in sheet.columns:
                        sheet[col] = None
                # 重新排序列
                sheet = sheet[list(all_columns)]
                standardized_sheets.append(sheet)
            
            # 合并所有子表
            self.df = pd.concat(standardized_sheets, ignore_index=True)
            
            # 标准化列名（中文）
            column_mapping = {
                '报警代码': '报警代码',
                '故障现象': '故障现象',
                '原因': '原因',
                '处理方法': '处理方法',
                '故障类型': '故障类型',
                '型号': '型号',
                '品牌': '品牌',
                '产品类型': '产品类型',
                '来源子表': '来源子表'
            }
            
            # 重命名列
            for old_col, new_col in column_mapping.items():
                if old_col in self.df.columns:
                    self.df.rename(columns={old_col: new_col}, inplace=True)
            
            # 检查必要的列是否存在
            required_columns = ['报警代码', '故障现象', '原因', '处理方法', '故障类型', '型号']
            missing_columns = [col for col in required_columns if col not in self.df.columns]
            
            if missing_columns:
                self.error_message = f"Excel文件中缺少必要的列: {missing_columns}"
                st.error(self.error_message)
                return False
            
            # 关键修改：确保所有列的数据类型一致
            # 先填充空值
            self.df = self.df.fillna('未知')

            if '型号' in self.df.columns:
                self.df['型号'] = self.df['型号'].astype(str).str.strip()
            
            # 关键修改：保留报警代码的原始格式，但添加规范化版本用于比较
            if '报警代码' in self.df.columns:
                # 保留原始报警代码
                self.df['报警代码_原始'] = self.df['报警代码']
                # 创建规范化版本用于比较
                self.df['报警代码_规范化'] = self.df['报警代码'].apply(normalize_alarm_code)
            
            # 数据加载成功后，设置加载状态
            self.data_loaded = True
            
            return True
            
        except Exception as e:
            self.data_loaded = False
            self.error_message = f"加载数据时出错: {e}"
            st.error(self.error_message)
            return False

    def search_by_pandas(self, model=None, alarm_code=None, limit=1000):
        """使用pandas进行精确查询"""
        if not self.data_loaded or self.df is None:
            return pd.DataFrame()
        
        try:
            # 构建筛选条件
            condition = pd.Series([True] * len(self.df))
            
            if model and model != "请选择":
                condition = condition & (self.df['型号'] == model)
            
            if alarm_code:
                # 规范化用户输入的报警代码
                normalized_alarm_code = normalize_alarm_code(alarm_code)
                # 使用规范化版本进行比较
                condition = condition & (self.df['报警代码_规范化'] == normalized_alarm_code)
            
            # 应用筛选条件
            result = self.df[condition].copy()
            
            # 限制结果数量
            if len(result) > limit:
                result = result.head(limit)
            
            return result
        except Exception as e:
            st.error(f"Pandas查询错误: {e}")
            return pd.DataFrame()

class SemanticSearcher:
    """语义搜索类"""
    def __init__(self, df):
        # 复制数据
        self.df = df.copy()
        self.embedding_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

        def clean_text(x):
            if pd.isna(x): return ""
            s = str(x).strip()
            return "" if s.lower() in ["未知", "nan", "none", ""] else s

        # 预计算向量
        texts_ph = self.df['故障现象'].apply(clean_text).tolist()
        texts_rs = self.df['原因'].apply(clean_text).tolist()

        self.phenomenon_embeddings = self.embedding_model.encode(texts_ph)
        self.reason_embeddings = self.embedding_model.encode(texts_rs)
    
    def semantic_search(self, query_text, candidate_indices=None, top_k=5, min_similarity=0.3):
        """语义相似度搜索 - 同时在故障现象和原因中搜索"""
        if not query_text.strip():
            return []

        query_embedding = self.embedding_model.encode([query_text])

        if candidate_indices is not None:
            # 使用DataFrame索引进行筛选
            candidate_mask = self.df.index.isin(candidate_indices)
            phenomenon_embeddings = self.phenomenon_embeddings[candidate_mask]
            reason_embeddings = self.reason_embeddings[candidate_mask]
            candidate_df = self.df[candidate_mask]
        else:
            phenomenon_embeddings = self.phenomenon_embeddings
            reason_embeddings = self.reason_embeddings
            candidate_df = self.df
        
        # 计算与故障现象的相似度
        phenomenon_similarities = cosine_similarity(query_embedding, phenomenon_embeddings)[0]
        
        # 计算与原因的相似度
        reason_similarities = cosine_similarity(query_embedding, reason_embeddings)[0]
        
        # 取两者中的较高值作为最终相似度
        similarities = np.maximum(phenomenon_similarities, reason_similarities)
        
        # 获取TopK结果
        top_indices = np.argsort(similarities)[-top_k:][::-1]
        top_sims = similarities[top_indices]
    
        results = []
        for idx, sim in zip(top_indices, top_sims):
            if sim < min_similarity:
                continue
            # 获取原始行
            if candidate_indices is not None:
                # 如果有限定候选集，需要映射回原始索引
                original_idx = candidate_df.iloc[idx].name
            else:
                original_idx = idx
                
            row = self.df.loc[original_idx]
            results.append({
                'index': int(original_idx),
                'similarity': float(sim),
                'data': row.to_dict()
            })

        results.sort(key=lambda x: x['similarity'], reverse=True)
        
        return results[:top_k]

# 初始化会话状态
if "messages" not in st.session_state:
    st.session_state.messages = []
if "api_key" not in st.session_state:
    st.session_state.api_key = st.secrets.get("DEEPSEEK_API_KEY", "")
if "base_url" not in st.session_state:
    st.session_state.base_url = "https://api.deepseek.com/v1"
if "loader" not in st.session_state:
    st.session_state.loader = RobustDataLoader()
    st.session_state.data_loaded = False
if "searcher" not in st.session_state:
    st.session_state.searcher = None
if "df" not in st.session_state:
    st.session_state.df = None
if "page" not in st.session_state:
    st.session_state.page = "聊天页面"
if "last_uploaded_file" not in st.session_state:
    st.session_state.last_uploaded_file = None
if "current_results" not in st.session_state:
    st.session_state.current_results = []
if "brand_model_mapping" not in st.session_state:
    st.session_state.brand_model_mapping = INITIAL_BRAND_MODEL_MAPPING
if "github_url" not in st.session_state:
    st.session_state.github_url = "https://github.com/lostintime-123/streamlit_test/raw/refs/heads/main/data.xlsx"

# 侧边栏导航
st.sidebar.title("🔧 数控设备故障诊断系统")
page = st.sidebar.radio("导航", ["聊天页面", "数据展示", "使用说明"])

# API配置
st.sidebar.header("⚙️ API配置")
api_key = st.sidebar.text_input("大模型API密钥", value="", type="password")
base_url = st.sidebar.text_input("API基础URL", value=st.session_state.base_url)

if st.sidebar.button("保存API配置"):
    st.session_state.api_key = api_key
    st.session_state.base_url = base_url
    st.sidebar.success("API配置已保存")

# 设备筛选
st.sidebar.header("📋 设备筛选")

# 获取品牌列表
brands = list(st.session_state.brand_model_mapping.keys())
selected_brand = st.sidebar.selectbox("选择品牌", ["请选择"] + brands, index=0)

# 获取产品类型
product_types = []
if selected_brand != "请选择":
    product_types = list(st.session_state.brand_model_mapping[selected_brand].keys())
selected_product_type = st.sidebar.selectbox("选择产品类型", ["请选择"] + product_types, index=0)

# 获取型号
models = []
if selected_product_type != "请选择":
    models = st.session_state.brand_model_mapping[selected_brand][selected_product_type]
selected_model = st.sidebar.selectbox("选择具体型号*", ["请选择"] + models, index=0)

alarm_code = st.sidebar.text_input("报警代码（可选）", "")

# 数据上传
st.sidebar.header("📤 数据上传")

# 使用选项卡布局
data_tab1, data_tab2 = st.sidebar.tabs(["上传文件", "GitHub地址"])

with data_tab1:
    uploaded_file = st.file_uploader("上传故障数据Excel文件", type=["xlsx", "xls"], key="file_uploader")
    
    # 检查是否需要重新加载数据
    if uploaded_file is not None:
        # 检查是否是新的文件
        if uploaded_file != st.session_state.last_uploaded_file:
            st.session_state.last_uploaded_file = uploaded_file
            st.session_state.data_loaded = False
            
        if st.button("加载数据", key="load_uploaded"):
            try:
                if st.session_state.loader.load_from_excel(uploaded_file):
                    st.session_state.df = st.session_state.loader.df
                    st.session_state.data_loaded = True
                    
                    # 初始化语义搜索器
                    st.session_state.searcher = SemanticSearcher(st.session_state.df)
                    
                    # 从数据中提取品牌和型号映射
                    extracted_mapping = extract_brand_model_mapping(st.session_state.df)
                    if extracted_mapping:
                        st.session_state.brand_model_mapping = extracted_mapping
                        # st.success(f"数据加载成功！共 {len(st.session_state.df)} 条记录，已自动更新品牌和型号列表")
                    else:
                        st.session_state.brand_model_mapping = INITIAL_BRAND_MODEL_MAPPING
                        # st.success(f"数据加载成功！共 {len(st.session_state.df)} 条记录，但未能提取品牌和型号信息，使用默认映射")
                    
                    st.rerun()
                else:
                    st.error("数据加载失败")
            except Exception as e:
                st.error(f"数据加载失败: {e}")
        
        # 显示数据加载状态
        if st.session_state.data_loaded:
            st.success("数据已加载")
        else:
            st.warning("数据未加载，请点击'加载数据'按钮")
    else:
        # 仅当用户真的点过上传区域但没有文件时，才重置
        if st.session_state.last_uploaded_file is not None:
            st.session_state.data_loaded = False
            st.session_state.last_uploaded_file = None

with data_tab2:
    github_url = st.text_input(
        "GitHub文件地址", 
        value=st.session_state.github_url,
        placeholder="例如: https://github.com/用户名/项目名/raw/refs/heads/main/文件名.xlsx",
        key="github_url_input"
    )
    
    if st.button("从GitHub加载", key="load_github"):
        if not github_url:
            st.error("请输入GitHub文件地址")
        else:
            try:
                # 验证URL格式
                if not github_url.startswith(('http://', 'https://')):
                    st.error("请输入有效的URL地址")
                elif 'raw' not in github_url:
                    # 如果用户提供了普通的GitHub URL，尝试转换为raw URL
                    st.warning("建议使用包含raw格式的URL")
                
                # 显示加载进度
                with st.spinner("加载GitHub文件..."):
                    # 添加请求头模拟浏览器访问
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
                    }
                    
                    # 发送请求获取文件
                    response = requests.get(github_url, headers=headers)
                    
                    # 检查响应状态
                    if response.status_code != 200:
                        st.error(f"下载失败，HTTP状态码: {response.status_code}")
                        st.error(f"响应内容: {response.text[:200]}...")
                        st.stop()
                    
                    # 检查内容类型
                    content_type = response.headers.get('content-type', '')
                    if 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' not in content_type and \
                       'application/octet-stream' not in content_type:
                        st.warning(f"下载的内容类型可能不是Excel文件: {content_type}")
                    
                    # 将内容转换为文件对象
                    file_obj = BytesIO(response.content)
                    
                    # 添加调试信息
                    st.info(f"下载成功，文件大小: {len(response.content)} 字节")
                    
                    # 创建一个新的数据加载器实例，确保状态正确
                    new_loader = RobustDataLoader()
                    
                    # 加载数据 - 添加调试信息
                    load_success = new_loader.load_from_excel(file_obj)
                    
                    if load_success:
                        # 更新会话状态
                        st.session_state.loader = new_loader
                        st.session_state.df = new_loader.df
                        st.session_state.data_loaded = True
                        st.session_state.github_url = github_url
                        
                        # 初始化语义搜索器
                        st.session_state.searcher = SemanticSearcher(st.session_state.df)
                        
                        # 从数据中提取品牌和型号映射
                        extracted_mapping = extract_brand_model_mapping(st.session_state.df)
                        # st.info(f"提取的品牌型号映射: {extracted_mapping}")
                        
                        if extracted_mapping:
                            st.session_state.brand_model_mapping = extracted_mapping
                            # 使用toast显示成功消息
                            # st.info(f"数据加载成功！共 {len(st.session_state.df)} 条记录，已自动更新品牌和型号列表", icon="✅")
                        else:
                            st.session_state.brand_model_mapping = INITIAL_BRAND_MODEL_MAPPING
                            # st.info(f"数据加载成功！共 {len(st.session_state.df)} 条记录，但未能提取品牌和型号信息，使用默认映射", icon="✅")
                        
                        # 强制刷新页面
                        st.rerun()
                    else:
                        st.error("数据加载失败")
                        # 显示加载器的错误信息（如果有）
                        if hasattr(new_loader, 'error_message') and new_loader.error_message:
                            st.error(f"错误详情: {new_loader.error_message}")
                            
            except requests.exceptions.RequestException as e:
                st.error(f"下载文件失败: {e}")
            except Exception as e:
                st.error(f"加载数据时出错: {e}")

    if st.session_state.github_url and st.session_state.data_loaded:
        st.success(f"已从GitHub加载数据: {st.session_state.github_url}")
        
# 会话管理
st.sidebar.header("💬 会话管理")
if st.sidebar.button("清除聊天记录"):
    st.session_state.messages = []
    st.rerun()

# 页面内容
if page == "聊天页面":
    st.title("💬 故障诊断聊天窗口")
    
    # 显示聊天历史
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            # 显示回复内容
            st.markdown(message["content"])
            
            # 显示相关文档（如果有）- 放在回复内容后面
            if message["role"] == "assistant" and "documents" in message and message["documents"]:
                with st.expander(f"📄 查看相关文档 ({len(message['documents'])} 条)", expanded=False):
                    for i, doc in enumerate(message["documents"], 1):
                        st.markdown(f"**文档 {i}** (相似度: {doc.get('similarity', 0):.3f})")
                        st.markdown(f"**型号**: {doc['data'].get('型号', '未知')}")
                        st.markdown(f"**报警代码**: {doc['data'].get('报警代码_原始', '未知')}")
                        st.markdown(f"**故障现象**: {doc['data'].get('故障现象', '未知')}")
                        st.markdown(f"**原因**: {doc['data'].get('原因', '未知')}")
                        st.markdown(f"**处理方法**: {doc['data'].get('处理方法', '未知')}")
                        st.markdown(f"**故障类型**: {doc['data'].get('故障类型', '未知')}")
                        st.markdown("---")
    
    # 聊天输入
    if prompt := st.chat_input("描述您遇到的故障问题..."):
        # 检查是否选择了型号
        if selected_model == "请选择":
            st.error("请先选择设备型号")
            st.stop()
        
        # 检查数据是否加载
        if not st.session_state.data_loaded or st.session_state.df is None or st.session_state.searcher is None:
            st.error("请先上传并加载故障数据")
            st.stop()
        
        # 添加用户消息到聊天历史
        st.session_state.messages.append({"role": "user", "content": prompt})
        
        # 显示用户消息
        with st.chat_message("user"):
            st.markdown(prompt)
        
        # 生成助手回复
        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            
            try:
                # 第一步：检索文档
                # 使用pandas进行精确筛选
                pandas_results = st.session_state.loader.search_by_pandas(
                    model=selected_model,
                    alarm_code=alarm_code if alarm_code else None,
                    limit=1000  # 提高限制，确保获取所有匹配记录
                )
                
                # 初始化 results 变量
                results = []
                candidate_indices = None
                search_info = ""
                
                # 调试信息
                debug_info = f"查询参数: 型号={selected_model}, 报警代码={alarm_code}\n"
                debug_info += f"规范化报警代码: {normalize_alarm_code(alarm_code) if alarm_code else '无'}\n"
                debug_info += f"Pandas查询结果行数: {len(pandas_results)}\n"
                
                if not pandas_results.empty:
                    # 使用DataFrame索引作为候选集
                    candidate_indices = pandas_results.index.tolist()
                    search_info = f"根据筛选条件找到 {len(pandas_results)} 条记录"
                    debug_info += f"找到记录: {len(pandas_results)} 条\n"
                    
                    # 关键修改：根据文档数量决定是否进行语义检索
                    if len(pandas_results) > 5:
                        search_info += "，正在进行语义搜索以找到最相关的结果"
                        debug_info += "进行语义搜索（文档数量 > 5）\n"
                        results = st.session_state.searcher.semantic_search(
                            prompt, candidate_indices, top_k=5  # 取前5个结果
                        )
                    else:
                        search_info += "，文档数量较少，直接返回所有结果"
                        debug_info += "直接返回所有结果（文档数量 ≤ 5）\n"
                        # 直接将pandas结果转换为与语义搜索相同的格式
                        for idx in candidate_indices:
                            row = st.session_state.df.loc[idx]
                            results.append({
                                'index': int(idx),
                                'similarity': 1.0,  # 设置为最高相似度
                                'data': row.to_dict()
                            })

                else:
                    # 如果没有精确匹配结果
                    if alarm_code:
                        search_info = f"没有找到型号 '{selected_model}' 和报警代码 '{alarm_code}' 的匹配记录"
                        debug_info += f"没有找到型号 '{selected_model}' 和报警代码 '{alarm_code}' 的匹配记录\n"
                    else:
                        search_info = f"没有找到型号 '{selected_model}' 的记录"
                        debug_info += f"没有找到型号 '{selected_model}' 的记录\n"
                
                # 保存当前结果
                st.session_state.current_results = results
                
                # #调试信息
                # debug_info += f"最终结果数量: {len(results)}\n"
                # if results:
                #     debug_info += f"第一个结果的报警代码: {results[0]['data'].get('报警代码_原始', '未知')}\n"
                #     debug_info += f"第一个结果的相似度: {results[0].get('similarity', 0):.3f}\n"
                
                # # 输出调试信息
                # st.sidebar.text_area("调试信息", debug_info, height=200)
                
                # if not results:
                #     full_response = f"{search_info}\n\n抱歉，没有找到相关的故障信息。请尝试提供更详细的描述或检查筛选条件。"
                #     message_placeholder.markdown(full_response)
                #     st.session_state.messages.append({
                #         "role": "assistant", 
                #         "content": full_response,
                #         "documents": []
                #     })
                #     st.stop()
                
                # 第二步：显示文档（固定位置，不会随流式输出移动）
                # 使用单独的容器显示文档
                doc_container = st.container()
                with doc_container:
                    with st.expander(f"📄 查看相关文档 ({len(results)} 条)", expanded=False):
                        for i, result in enumerate(results, 1):
                            data = result['data']
                            st.markdown(f"**文档 {i}** (相似度: {result.get('similarity', 0):.3f})")
                            st.markdown(f"**型号**: {data.get('型号', '未知')}")
                            st.markdown(f"**报警代码**: {data.get('报警代码_原始', '未知')}")
                            st.markdown(f"**故障现象**: {data.get('故障现象', '未知')}")
                            st.markdown(f"**原因**: {data.get('原因', '未知')}")
                            st.markdown(f"**处理方法**: {data.get('处理方法', '未知')}")
                            st.markdown(f"**故障类型**: {data.get('故障类型', '未知')}")
                            st.markdown("---")
                
                # 第三步：构建提示词，将文档内容交给大模型
                context = "相关的故障信息：\n"
                if results:
                    for i, result in enumerate(results, 1):
                        data = result['data']
                        context += f"\n--- 结果 {i} (相似度: {result['similarity']:.3f}) ---\n"
                        context += f"型号: {data.get('型号', '未知')}\n"
                        context += f"报警代码: {data.get('报警代码_原始', '未知')}\n"
                        context += f"故障现象: {data.get('故障现象', '未知')}\n"
                        context += f"原因: {data.get('原因', '未知')}\n"
                        context += f"处理方法: {data.get('处理方法', '未知')}\n"
                        context += f"故障类型: {data.get('故障类型', '未知')}\n"
                else:
                    context += "\n未找到相关故障信息。\n"
                
                prompt_with_context = f"""
                用户查询: {prompt}
                
                {context}
                
                请根据以上故障信息，为用户提供专业的故障诊断和处理建议：
                1. 首先确认故障类型和可能的原因。如果有多个可能的原因，按可能性排序说明。
                2. 提供具体的处理步骤和方法
                3. 用专业但易懂的语言回答
                
                注意：只基于提供的信息回答，不要编造不存在的信息。
                """
                
                # 第四步：调用大模型生成回复
                # 初始化DeepSeek客户端
                client = OpenAI(
                    api_key=st.session_state.api_key,
                    base_url=st.session_state.base_url
                )
                
                # 调用DeepSeek生成回答
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {
                            "role": "system", 
                            "content": "你是一个专业的数控设备故障诊断专家，根据提供的故障信息给出准确专业的回答。"
                        },
                        {"role": "user", "content": prompt_with_context}
                    ],
                    temperature=0.1,
                    stream=True
                )
                
                # 流式显示回复
                for chunk in response:
                    if chunk.choices[0].delta.content is not None:
                        full_response += chunk.choices[0].delta.content
                        message_placeholder.markdown(full_response + "▌")
                
                message_placeholder.markdown(full_response)
                
            except Exception as e:
                full_response = f"生成回答时出错: {str(e)}"
                message_placeholder.markdown(full_response)
                results = []  # 确保 results 被定义
            
            # 添加助手消息到聊天历史
            st.session_state.messages.append({
                "role": "assistant", 
                "content": full_response,
                "documents": results
            })

elif page == "数据展示":
    st.title("📊 数据展示")
    
    # 检查数据加载状态 - 同时检查loader和df状态
    if not hasattr(st.session_state, 'data_loaded') or not st.session_state.data_loaded or \
       not hasattr(st.session_state, 'df') or st.session_state.df is None:
        st.warning("请先上传并加载数据")
    else:
        st.success(f"已加载 {len(st.session_state.df)} 条故障记录")
        
        # 显示数据表格 - 只显示原始报警代码，不显示规范化版本
        display_df = st.session_state.df.copy().reset_index(drop=True)

        if '序号' in display_df.columns:
            display_df = display_df.drop('序号', axis=1)
        
        # 移除规范化报警代码列
        if '报警代码_规范化' in display_df.columns:
            display_df = display_df.drop('报警代码_规范化', axis=1)
        
        # 重命名原始报警代码列为报警代码
        if '报警代码_原始' in display_df.columns:
            display_df = display_df.rename(columns={'报警代码_原始': '报警代码'})
        
        # 确保所有列都是字符串类型
        for col in display_df.columns:
            display_df[col] = display_df[col].astype(str)
        
        # 修复重复列名问题
        # 删除重复的列（如果有）
        display_df = display_df.loc[:, ~display_df.columns.duplicated()]
        
        st.dataframe(display_df, width=1200, height=600)
        
        # 显示统计信息
        st.subheader("数据统计")
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("总记录数", len(st.session_state.df))
        
        with col2:
            unique_models = st.session_state.df['型号'].nunique()
            st.metric("设备型号数", unique_models)
        
        with col3:
            # 使用原始报警代码列进行统计
            if '报警代码_原始' in st.session_state.df.columns:
                unique_alarm_codes = st.session_state.df['报警代码_原始'].nunique()
            else:
                unique_alarm_codes = st.session_state.df['报警代码'].nunique()
            st.metric("报警代码数", str(unique_alarm_codes))
        
        # 显示型号分布
        st.subheader("设备型号分布")
        model_counts = st.session_state.df['型号'].value_counts()
        model_counts.index = model_counts.index.astype(str)
        st.bar_chart(model_counts.head(10))
        
        # 显示故障类型分布
        st.subheader("故障类型分布")
        fault_type_counts = st.session_state.df['故障类型'].value_counts()
        fault_type_counts.index = fault_type_counts.index.astype(str)
        st.bar_chart(fault_type_counts)

elif page == "使用说明":
    st.title("📖 使用说明")
    
    st.markdown("""
    ## 系统介绍
    
    这是一个专业的数控设备故障诊断系统，可以帮助您快速诊断和解决设备故障问题。
    
    ## 使用步骤
    
    1. **选择设备型号** (必需)
       - 在侧边栏通过下拉菜单选择品牌、产品类型和具体型号
       
    2. **输入报警代码** (可选)
       - 如果您知道报警代码，可以在侧边栏输入
       - 系统会自动处理报警代码格式（去除开头多余的0）
       
    3. **上传数据**
       - 在侧边栏上传包含故障信息的Excel文件
       - 或者输入GitHub文件地址（需要是原始文件地址）
       - 点击"加载数据"按钮
       - 系统会自动从数据中提取品牌和型号信息并更新下拉选项
       
    4. **描述故障**
       - 在聊天页面描述您遇到的故障问题
       
    ## 搜索逻辑
    
    1. **只有型号**：在该型号的所有记录中进行语义搜索，取相似度最高的前5条
    2. **型号+报警代码**：在同时匹配型号和报警代码的记录中进行语义搜索，取相似度最高的前5条
       
    ## 注意事项
    
    - 设备型号是必选项，否则无法进行搜索
    - 报警代码会自动规范化处理（去除开头多余的0）
    - 确保Excel文件包含必要的列：序号、报警代码、故障现象、原因、处理方法、故障类型、型号
    - 如果Excel文件中包含品牌和产品类型列，系统会自动提取这些信息并更新下拉选项
    - GitHub文件地址需要是原始文件地址（raw格式），例如：https://github.com/用户名/项目名/raw/refs/heads/main/文件名.xlsx
    """)

# 运行说明
# st.sidebar.markdown("---")
# st.sidebar.info("""
# **运行说明**:
# 1. 安装依赖: `pip install streamlit pandas openpyxl sentence-transformers scikit-learn openai`
# 2. 运行应用: `streamlit run app.py`
# """)
