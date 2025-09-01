# streamlit_test

由于数控维修文档较多，这个代码的大概思路是根据设备型号和报警代码筛选文档，再做语义检索，然后调用deepseek api生成回答。

测试过unsloth微调，但数据量太少了，微调没有多大意义。

数据文档是data.xlsx。

使用steamlit cloud部署。
