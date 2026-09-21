# HTTP API

所有端点前缀 `/api`，请求体为 JSON，最大 2 MB。错误格式为 `{"error":"说明"}`。认证采用 `research_session` HttpOnly Cookie；浏览器写请求要求同源。未登录返回 401、无权限返回 403、跨用户项目或任务返回 404、冲突返回 409。

## 账户与系统

| 方法 | 路径 | 内容 |
|---|---|---|
| GET | /health | 存活检查，公开 |
| GET | /bootstrap | 是否需要首次初始化、注册开关，公开 |
| POST | /auth/setup | 首位管理员：email、name、password；远程需 bootstrap_token |
| POST | /auth/login | email、password |
| POST | /auth/register | 可选注册；需 ALLOW_REGISTRATION=1 且已初始化 |
| POST | /auth/logout | 删除当前会话 |
| GET | /me | 当前账户基本信息 |
| PATCH | /profile | name |
| GET | /overview | 个人项目、最近 100 个任务、统计、审批队列 |
| GET | /settings | 非敏感模型配置与能力状态 |
| PUT | /settings | 管理员：base_url、model、max_calls、max_output_tokens、allow_arxiv |
| GET | /admin/users | 管理员：账户列表，不含密码哈希 |
| POST | /admin/users | 管理员创建成员：email、name、password |

## 项目、资料与数据

| 方法 | 路径 | 内容 |
|---|---|---|
| POST | /demo | 创建原创演示资料项目与合成数据集，不自动运行 |
| POST | /projects | name、question、description（可选） |
| GET | /projects/{id} | 项目、资料、数据集、任务与调度 |
| PATCH | /projects/{id} | name、question、archived（可选） |
| DELETE | /projects/{id} | 必须先归档且没有活动任务；级联删除 |
| POST | /projects/{id}/sources | title、content、url（可选）；或 pdf_base64 |
| DELETE | /projects/{id}/sources/{source_id} | 删除资料库项；任务快照保留 |
| POST | /projects/{id}/datasets | name、content（CSV 原文）；校验后写入 |
| DELETE | /projects/{id}/datasets/{dataset_id} | 删除数据集；任务快照保留 |
| GET | /projects/{id}/messages | 原文定位问答记录 |
| POST | /projects/{id}/messages | content；返回关键词检索原文摘录 |
| PUT | /projects/{id}/schedule | mode、interval_hours、enabled |

## 任务

| 方法 | 路径 | 内容 |
|---|---|---|
| POST | /projects/{id}/runs | mode: demo/live；可选 dataset_id、goal、methods、k |
| GET | /runs/{id} | 状态、角色输出、步骤、审批、资料快照、交付物目录 |
| GET | /runs/{id}/events?after=0 | ID 增量事件，每页最多 500，使用最后 ID 翻页 |
| POST | /runs/{id}/cancel | 取消请求；正在执行的网络请求返回后生效 |
| POST | /runs/{id}/retry | 仅失败/取消任务，沿用检查点与剩余调用额度 |
| POST | /runs/{id}/approval | decision: approved/rejected；可选 note |
| GET | /runs/{id}/export | ZIP 交付包 |
| GET | /artifacts/{artifact_id} | 单个文件下载 |

前端使用轮询读取持久事件与任务状态，不使用 SSE。刷新页面可以继续查看历史记录。

## Curl 示例

```bash
curl -c cookies.txt -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"your-password"}' \
  http://127.0.0.1:8000/api/auth/login

curl -b cookies.txt -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8000/api/demo

# 将返回的项目与数据集 ID 填入：
curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"mode":"demo","dataset_id":"ds_..."}' \
  http://127.0.0.1:8000/api/projects/prj_.../runs

curl -b cookies.txt http://127.0.0.1:8000/api/runs/run_...

curl -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"decision":"approved","note":"参数已核对"}' \
  http://127.0.0.1:8000/api/runs/run_.../approval
```

示例中的密码由使用者自行替换，不使用仓库预设密码。会话 Cookie 是凭据，不应提交到 Git。
