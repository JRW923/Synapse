---
name: pytest
triggers: pytest, 单测, 单元测试
task_types: test
---
使用 pytest 编写与运行测试时的建议：

- 测试文件命名 `test_*.py`，测试函数命名 `test_*`。
- 用 `tmp_path` fixture 处理临时文件，不要在仓库里留垃圾。
- 用 `pytest -q` 运行；失败时用 `-x` 在首个失败处停下。
- 断言要具体：优先 `assert result == expected` 而非仅 `assert result`。
- 需要 mock 外部依赖时用 `unittest.mock` 或 `pytest-mock` 的 `mocker` fixture。
