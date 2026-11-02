import os
import tempfile

# 测试不写入仓库内的 backend/data：知识库的上传文档与案例落到临时目录
os.environ.setdefault("HEATNET_DATA_DIR", tempfile.mkdtemp(prefix="heatnet-test-"))
