# 防作弊与完整性校验策略

## 定位

本项目只能控制客户端行为，不能修改第三方 Mars 服务端。因此这里的“防作弊校验”是运行前和提交前的强约束，以及可审计的执行记录，不会伪装成服务端内部风控代码。

参考项目中的约束被映射为：

| 参考机制 | Mars 实现 |
| --- | --- |
| 学习埋点 | 活动、试卷、题量和限时前置校验 |
| `logId/token` | 登录 token、用户身份和单次运行绑定 |
| 最短答题时间 | 模拟考和正式考试默认至少 613 秒 |
| 单线程提交 | 单进程文件锁 |
| 错题补库 | 答案覆盖率检查与错题学习 |

## 执行顺序

```text
获取活动列表
  -> 校验登录用户
  -> 获取运行锁
  -> 校验活动类型和 userActivityId
  -> preparePaper
  -> 校验题量、限时、分数、次数
  -> startPaper
  -> 校验首批题目
  -> 每题校验 ID、题型、选项和答案归属
  -> recordQuestion
  -> 校验题库覆盖率
  -> 等待考试最低总用时
  -> submitPaper
  -> 校验答对/答错总数和分数
  -> 错题学习
  -> 写入审计结束事件
  -> 释放运行锁
```

## 校验项

| 编码 | 触发条件 |
| --- | --- |
| `session-token` | token 缺失或明显无效 |
| `session-user` | 登录用户身份缺失 |
| `activity-type` | 活动类型不是 1/2/3/4 |
| `activity-id` | 缺少 `userActivityId` |
| `paper-size` | 题量不是正数 |
| `paper-time` | 考试限时不是正数 |
| `paper-score` | 合格分超出总分 |
| `attempt-limit` | 已用次数达到总次数 |
| `question-id` | 题目缺少 ID |
| `question-duplicate` | 同一试卷出现重复题目 ID |
| `question-type` | 题型不受支持 |
| `question-options` | 选项不足、ID 重复或文本为空 |
| `answer-empty` | 没有可提交答案 |
| `answer-option` | 答案 ID 不属于当前题目 |
| `answer-count` | 单选/判断答案数量不为 1 |
| `server-correct` | 严格模式尝试使用服务端 `isCorrect` |
| `bank-coverage` | 未命中比例超过默认 5% 阈值 |
| `submit-too-fast` | 早于最短考试时长 |
| `submit-too-slow` | 超过试卷限时 |
| `result-count` | 答对数和答错数之和不等于题量 |
| `result-score` | 服务端成绩不在 0 到 100 |
| `run-lock` | 已有一个任务占用运行锁 |

## 最短时长

```text
minimum = max(
  exam_min_seconds,
  questionNum * exam_min_seconds_per_question
)
minimum = min(minimum, answerTime * 60)
```

默认设置考试最低总用时：

```text
exam_min_seconds = 613
exam_min_seconds_per_question = 0
```

实际模拟考中，100 题用 119 秒完成后服务端正常返回 100 分。这个速度在业务上过于夸张，因此客户端将正式考试的单题节奏调整为随机 5～7 秒，并保持 613 秒最低总用时。

## Mars 实际适配

只采用 Mars 前端和真实接口能够证明存在的约束：

- 登录和 token：所有考试接口未登录时返回业务 401。
- 活动和试卷：`preparePaper/startPaper` 使用 `userActivityId`。
- 题目结构：`id/type/title/optionList`。
- 答案归属：`recordQuestion` 必须使用当前题目的选项 ID。
- 交卷结果：服务端返回 `rightNum/errorNum/score`。
- 限时：试卷返回 `answerTime`，前端倒计时并在超时后交卷。
- 节奏：正式考试默认每题随机等待 5～7 秒，并保持 613 秒最低总用时。

没有采用参考项目的学习埋点、单元测试 token 和固定最短等待，因为 Mars 不存在对应接口，且实测行为与应用这些限制相冲突。

## 审计链

每次关键事件记录：

```json
{
  "sequence": 1,
  "timestamp": 1792593000.123,
  "event": "question_recorded",
  "data": {
    "question_id": "example",
    "question_type": 1,
    "answer_count": 1,
    "match_kind": "exact",
    "confidence": 1.0
  },
  "previous_hash": "000...",
  "hash": "..."
}
```

`hash` 是前一条记录哈希和当前记录规范化 JSON 的 SHA-256。删除、插入或修改中间记录都会使后续链校验失败。

验证：

```bash
python3 run.py audit-verify
```

该链没有服务端私钥，因此是本地可检测篡改的审计日志，不是不可伪造的远程证明。
