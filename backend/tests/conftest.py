import os
import tempfile

# 测试不写入仓库内的 backend/data：知识库的上传文档与案例落到临时目录
os.environ.setdefault("HEATNET_DATA_DIR", tempfile.mkdtemp(prefix="heatnet-test-"))

# 测试默认不加载真实向量模型（避免下载与耗时）；向量相关逻辑用测试替身覆盖，真实模型测试在可用时单独运行
os.environ.setdefault("HEATNET_EMBED", "off")
