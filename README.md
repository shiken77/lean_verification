# Lean Verification Loop Prototype

这个小应用演示一个有边界的 autonomous-agent 验证闭环：

1. 用户用自然语言输入函数规范；
2. Agent 先把要求翻译成 Lean 正确性定理；
3. 用户确认后，服务端锁定这份形式化规范；
4. Agent 生成 Lean 函数和证明；
5. Lean 按锁定的定理检查证明；
6. 失败时，Lean 的错误信息会返回给 Agent；
7. Agent 修复后再次验证，直到通过或达到重试上限。

## 先运行内置演示

项目使用自己的 `.elan` 目录，不会修改全局 Lean 配置。

```bash
./install_lean.sh
python3 app.py
```

然后打开 <http://127.0.0.1:8765>，选择“内置演示”。先确认形式化定理，再运行验证。演示会故意在第一轮生成错误实现，第二轮修复，用来展示 FAIL → feedback → PASS。

## 启用真正的 AI

先在启动应用的终端中设置环境变量：

```bash
export DEEPSEEK_API_KEY="你的 API key"
export DEEPSEEK_MODEL="deepseek-chat"
python3 app.py
```

网页中选择“DeepSeek API”。Agent 会先生成供用户确认的形式化规范，再生成 Lean 源码。

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

网页中的 **Run 15-case test set** 会运行相同的测试集：

- 5 个正常可证明任务；
- 5 个故意错误的实现；
- 3 个证明绕过尝试；
- 2 个需要澄清的模糊需求。

固定测试案例在 `benchmark_cases.py`，测试入口在 `tests/test_prototype.py`。

## 这个原型证明了什么？

PASS 表示：Lean 接受了“这段函数满足用户确认并锁定的形式化规范”的证明。它不表示：

- 用户的自然语言意图一定被形式化规范准确表达；
- 整个软件的所有行为都正确；
- 生成的代码可以不经安全隔离就部署到生产环境。

原型会拒绝 `sorry`、`admit`、`axiom`、`unsafe` 等绕过证明或扩大执行能力的结构，并为 Lean 设置超时。但这仍然只是研究型原型，不是用于执行不可信代码的安全沙箱。
