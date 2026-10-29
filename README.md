# mars-deimos-skip

面向 Mars Deimos 学习平台的本地 Python 客户端，用于整理登录、活动选择、组卷、答题、交卷、历史记录和错题回收流程。

本项目不是平台官方客户端，仅用于本地学习、接口研究、软件测试和自有账号的辅助操作。使用前请确认所在学校与平台的使用规则，并自行承担账号、成绩和数据风险。

> 源码公开不等于允许倒卖。禁止将本项目或其修改版用于收费代刷、出售源码、出售账号服务、闭源转售或冒充原创项目。转载或二次开发请保留项目来源和作者链接。

## 功能

- 交互式登录，支持学校选择、验证码和本地会话复用。
- 支持顺序练习、随机练习、模拟考试和正式考试。
- 本地题库匹配，支持单选题、多选题和判断题。
- 支持选项顺序变化后的选项文本映射。
- 正式考试提供“全对”和“拟人”两种作答策略。
- 拟人模式随机选择 10 道不同题目，构造有效错误答案。
- 交卷前保持可配置的答题节奏，避免请求过于集中。
- 交卷后检查服务端实际成绩、错题数量和作答落库状态。
- 支持错题答案回收并合并到本地学习题库。
- 支持自动退出登录、会话归档和验证码临时文件清理。
- 提供离线单元测试、协议文档和完整性审计工具。

## 快速开始

### Windows

需要 Python 3.10 或更高版本。

```powershell
git clone https://github.com/wzqykh/mars-deimos-skip.git
cd mars-deimos-skip
python main.py
```

首次运行会自动创建 `.venv` 并安装依赖。

也可以使用仓库中的启动脚本：

```powershell
.\run.bat
```

### Linux / macOS

```bash
git clone https://github.com/wzqykh/mars-deimos-skip.git
cd mars-deimos-skip
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp config.example.json config.json
python3 run.py --help
```

## 使用流程

直接运行：

```powershell
python main.py
```

默认入口会：

```text
检查运行环境
→ 登录或复用本地会话
→ 列出练习与考试活动
→ 选择活动
→ 匹配本地题库
→ 按策略答题
→ 等待最低考试时长
→ 交卷并验证服务端结果
→ 考试完成后自动退出并归档会话
```

指定活动：

```powershell
python main.py auto --activity-index 2
```

只执行试卷预检，不开始答题：

```powershell
python3 run.py run --activity-index 0 --dry-run
```

查看活动：

```powershell
python3 run.py list
```

回收当前活动最近一次错题答案：

```powershell
python3 run.py wrong --activity-index 0
```

主动退出并归档本地会话：

```powershell
python3 run.py logout
```

## 多账号并行

不同终端使用不同配置档时，会话和运行锁彼此隔离：

```powershell
# 终端 1
python main.py auto --profile account-a

# 终端 2
python main.py auto --profile account-b
```

也可以通过环境变量固定配置档：

```powershell
$env:MARS_PROFILE="account-a"
python main.py auto
```

每个配置档的会话、验证码、运行锁、审计和错题学习文件位于：

```text
.state/profiles/<profile>/
```

公共题库 `data/questions.jsonl` 仍然共用。不指定配置档时继续使用原来的 `.state/`。同一配置档仍只允许一个运行任务，避免同一账号重复开卷；不同配置档可以并行运行。

## 正式考试策略

选择正式考试后，默认会询问：

```text
[0] 全对（默认）
[1] 拟人模式：随机故意错 8～11 题
[2] 96 分模式：故意错 4 题
```

也可以直接通过参数指定：

```powershell
python main.py auto --activity-index 3 --answer-mode perfect
python main.py auto --activity-index 3 --answer-mode human
python main.py auto --activity-index 3 --answer-mode score96
```

拟人模式不会简单随机填入无效内容，而是根据题型构造合法但错误的答案：

- 单选题：选择另一个有效选项。
- 判断题：选择与正确答案相反的选项。
- 多选题：通过删除、增加或替换选项，构造不同于正确答案的有效组合。

交卷后会核对：

```text
计划故意错题数
→ 实际发送的错题数
→ 服务端统计的错题数
```

三者不一致时会明确提示，避免把异常低分误认为策略结果。

## 答题节奏与结果审计

正式考试默认参数：

```text
每题随机等待：5～7 秒
最低总用时：613 秒（10 分 13 秒）
```

项目会在交卷后读取服务端历史与错题数据，检查：

- 交卷结果是否存在。
- 答对数和答错数是否等于题目总数。
- 服务端是否保存了错题的 `selected` 选择。
- 拟人模式的计划错题数是否与服务端错题数一致。

如果服务端没有保存大量作答，项目会明确报错，而不会继续把低分结果当成正常完成。

## 登录与隐私

- 程序不会把密码写入运行记录；请勿把密码填入 `config.json` 或提交到仓库。
- 本地会话保存在 `.state/session.json`，登录 token 不进入 Git。
- 验证码图片和 HTML 大图页在使用后自动删除。
- 登录开始时自动清理此前遗留的临时验证码文件。
- 考试完成后可自动退出并归档会话。
- `.state/`、`config.json` 和 `data/learned.jsonl` 已加入 `.gitignore`；请勿手动提交任何本地运行产物。

请不要在 Issue、Pull Request 或 Discussions 中上传：

- 真实姓名、学号、手机号和学校内部账号。
- 登录 token、Cookie、密码或完整请求头。
- 含个人信息的成绩截图、验证码截图或网页截图。
- 完整平台数据库、学生名单或其他第三方隐私数据。

## 常用命令

```text
python main.py
python main.py auto
python main.py auto --activity-index 2
python main.py auto --activity-index 3 --answer-mode perfect
python main.py auto --activity-index 3 --answer-mode human
python main.py login
python main.py list
python main.py logout
```

Linux 下也可以使用：

```bash
./run.sh
./run.sh login
./run.sh list
./run.sh run --activity-index 0
```

## 配置

首次使用可以复制示例配置：

```bash
cp config.example.json config.json
```

常用字段：

| 字段 | 说明 |
| --- | --- |
| `profile` | 账号配置档名称；也可使用 `--profile` 或 `MARS_PROFILE` |
| `tenant` | 学校名称或租户代码；留空后交互选择 |
| `username` | 学号或账号；建议留空 |
| `password` | 不建议写入文件；可使用环境变量 |
| `exam_question_delay_min_seconds` | 正式考试每题等待下限，默认 5 秒 |
| `exam_question_delay_max_seconds` | 正式考试每题等待上限，默认 7 秒 |
| `formal_exam_mode` | 正式考试策略：`ask`、`perfect` 或 `human` |
| `deliberate_wrong_min` | 拟人模式故意错题数下限，默认 8 |
| `deliberate_wrong_max` | 拟人模式故意错题数上限，默认 11 |
| `score96_wrong_count` | 96 分模式故意错题数，默认 4 |
| `audit_submitted_answers` | 是否检查服务端作答落库状态 |
| `allow_partial_practice_submit` | 顺序练习未达到预期题数时是否仍允许交卷，默认关闭 |
| `exam_min_seconds` | 考试最低总用时，默认 613 秒 |
| `logout_after_exam` | 考试完成后是否自动退出登录 |
| `cleanup_captcha_files` | 是否自动删除验证码临时文件 |
| `save_run_log` | 是否保存完整运行记录 |
| `save_audit_log` | 是否保存逐题审计链 |

通过环境变量提供账号：

```powershell
$env:MARS_USERNAME="your-account"
$env:MARS_PASSWORD="your-password"
python main.py login
```

```bash
export MARS_USERNAME='your-account'
export MARS_PASSWORD='your-password'
python3 run.py login
```

## 项目结构

```text
main.py                   自动创建虚拟环境并启动程序
run.py                    命令行入口
mars_deimos/api.py        登录、接口请求、响应解包和会话持久化
mars_deimos/bank.py       题库索引、题目匹配和选项映射
mars_deimos/workflow.py   答题、考试策略、交卷、审计和错题回收
mars_deimos/guard.py      完整性校验、运行锁和审计哈希链
mars_deimos/cli.py        交互界面和子命令
mars_deimos/xlsx.py       XLSX 题库读取器
tests/                    离线单元测试
docs/API_MAP.md           接口与字段对照
docs/GUARD_POLICY.md      校验策略和错误码
data/questions.jsonl      示例题库
```

## 开发与测试

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

运行离线测试：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

运行项目验证脚本：

```bash
bash scripts/verify.sh
```

涉及网络、验证码和真实账号的完整流程不会被默认测试自动执行。

## 安全边界

本项目只控制本地客户端，不修改平台服务端，也不保证平台一定会接受请求。服务端可能随时调整：

- 登录方式、验证码和 token 规则。
- 活动类型、次数和考试时间限制。
- 题目、选项与答案结构。
- 请求频率限制和风控策略。
- 交卷后的成绩与历史记录格式。

遇到接口变化时，请先使用 `probe`、`list` 和 `--dry-run` 检查状态，不要在信息不完整时重复开卷。

## 使用与转载

欢迎提交 Issue 和 Pull Request，也欢迎基于本项目进行个人学习和非商业改进。

请保留项目来源、作者链接和贡献者信息。禁止将本项目或其主要代码用于：

- 收费代刷、代考或账号服务。
- 出售源码、安装包或修改版本。
- 闭源转售、贴牌发布或冒充原创。
- 批量收集、出售或公开他人个人信息。

如果你计划将本项目用于商业产品、组织内部服务或收费部署，请先联系维护者获得书面许可。

> 许可证类型将在公开发布前单独确认。若选择 OSI 认可的标准许可证，“禁止倒卖”更适合作为社区约定；若需要形成可执行的限制，应使用非商业源码许可证，并在仓库根目录添加对应 `LICENSE` 文件。

## 支持项目

如果这个项目对你有帮助，可以自愿支持维护工作。捐赠完全自愿，不影响功能，也不构成付费服务承诺。

<img src="docs/assets/wechat-pay.png" alt="微信收款码" width="320">

二维码仅保留支付码本体，不包含原图周围的昵称或附加文字。

## 致谢

- 感谢 [Scwizard/jiangsu-safety-platform-skip](https://github.com/Scwizard/jiangsu-safety-platform-skip) 提供流程设计和公开实现参考。
- 感谢所有参与测试、题库整理、问题反馈和文档改进的贡献者。

## 免责声明

本项目与 Mars Deimos 平台及其运营方无隶属关系。

使用者应自行确认其使用行为符合所在学校、平台和适用法律的要求。项目维护者不对账号、成绩、数据、服务中断或其他直接或间接损失承担责任。
