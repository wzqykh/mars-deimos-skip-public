# Mars Deimos API 映射

分析日期：2026-10-21

## 运行时

- 页面：`https://mars.mycourse.cn/deimos/#/home`
- 前端：Vue 2.7.16 + Vue Router 3 + Vuex 3 + Element UI 2
- HTTP：Axios 0.27.2，表单请求，`Content-Type: application/x-www-form-urlencoded;charset=utf8`
- API 根：`https://mars.mycourse.cn/marsapi`
- 鉴权：`Authorization: <token>`，不带 `Bearer`
- 业务响应：HTTP 通常为 200，内部 `status` 表示成功或鉴权失败

```json
{
  "status": 200,
  "message": "操作成功",
  "data": {
    "result": {}
  },
  "success": true
}
```

前端统一读取 `data.result`。若它是字符串，再执行一次 `JSON.parse`。

## 登录与用户

| 方法 | 路径 | 关键参数 |
| --- | --- | --- |
| POST | `/api/login/v1/listTenant` | 无 |
| POST | `/api/login/v1/getTenantConfig` | `tenantCode` |
| GET | `/api/login/v1/getCaptcha` | `timestamp` |
| POST | `/api/login/v1/login` | `tenantCode`、`username`、`encryptedPassword`、`captchaCode`、`timestamp` |
| POST | `/api/user/v1/index` | 无 |
| POST | `/api/login/v1/logout` | 无 |

密码处理：

```text
encryptedPassword = MD5(password).toUpperCase()
```

## 首页与活动

| 方法 | 路径 | 关键参数 |
| --- | --- | --- |
| POST | `/api/study/project/v1/index` | 无 |
| POST | `/api/simpleorder/v1/list` | `projectId` |

`project/index` 返回 `projectId`、`projectName`、`studyActivityList`、`examErrorState`、`rankState`、`isChuFu` 和 `subActivityType`。

## 练习和考试

`activityType=1/2` 使用 `practice`，`activityType=3/4` 使用 `exam`。

| 动作 | 路径后缀 | 参数 |
| --- | --- | --- |
| 预检 | `/preparePaper` | `userActivityId` |
| 开始/恢复 | `/startPaper` | `userActivityId` |
| 练习分页 | `/practice/v1/getNear` | `userActivityId`、`currentIndex`、`nearType` |
| 记录答案 | `/recordQuestion` | `userActivityId`、`questionId`、`answerIds`、`useTime`；练习另有 `currentIndex` |
| 交卷 | `/submitPaper` | `userActivityId` |
| 历史 | `/listHistory` | `userActivityId` |

`startPaper` 返回的核心字段：

```text
currentIndex
hasBefore
hasNext
name
answerTime
questionList[]
```

题目对象核心字段：

```text
id
type
title
optionList[]:
  id
  content
  selected
  isCorrect
```

答案编码：

```text
type=1  单选题 -> 单个 optionList[].id
type=2  多选题 -> 多个 optionList[].id，以逗号拼接
type=4  判断题 -> 单个 optionList[].id
```

## 错题

| 方法 | 路径 | 参数 |
| --- | --- | --- |
| POST | `/api/study/activity/practice/v1/listErrorQuestion.do` | `userExamId` |
| POST | `/api/study/activity/exam/v1/listErrorQuestion.do` | `userExamId` |
| POST | `/api/study/activity/errorbook/v1/listErrorBook.do` | `projectId`、`userId` |

普通历史记录的错题接口返回 `questionList`。项目错题本接口整体返回题目数组。`optionList[].isCorrect` 可直接用于把错题正确答案写入 `data/learned.jsonl`。

## 错误状态

| 内部状态 | 行为 |
| --- | --- |
| `401` | 前端提示登录超时并返回登录页 |
| `111` | 前端提示业务错误并返回首页 |
| `104` | 交卷页面提示后端返回的具体消息 |

HTTP 层无法只凭状态码区分业务成功和鉴权失败，客户端必须读取 JSON 内部状态。
