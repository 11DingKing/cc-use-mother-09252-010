# 国际职教合作成效核算

面向业务人员的纯服务端系统：登记指标定义、数据版本、换算规则与证据来源，
在指定观察期内生成**可复算**的项目结论。仅依赖 Python 标准库（WSGI + JSON 快照）。

## 核心规则

- **指标定义版本化**：每次更新生成新版本；报告生成时冻结所用定义版本，
  定义更新**不改写旧报告**。
- **迟到数据只形成新版本**：同一逻辑键（项目+指标+国家+口径+期间）的数据
  以递增 `version_no` 保存，旧值不覆盖；重新计算只追加新报告版本，并提供版本差异。
- **口径会签**：换算规则可要求多个必备机构全部同意后才生效；并发同意安全，
  驳回即失效。
- **规则回滚**：以新版本复制曾经生效的历史口径，历史版本与历史报告均不变。
- **可复算**：报告内含指标定义、换算规则、数据点与证据的完整快照及 SHA256 指纹；
  导出包可离线独立复算。
- **授权粒度**：`import / signoff / calc / review / read / admin`，
  并可收窄到具体项目；机构只能访问被授权的粒度。
- **计算任务**：支持幂等键（批次与任务分别命名）、状态 CAS 防并发执行、
  检查点即时落盘，崩溃后 `/resume` 断点恢复。

## 架构（端口与分层）

```
domain/          领域模型(models) 与纯计算引擎(engine)、领域错误
application/     端口(ports: Clock/IdGenerator/SnapshotStore)、鉴权(access)、
                 目录登记(catalog)、导入(importer)、计算(calc)、复核(review)、导出(export)
persistence/     内存库 + UoW（线程锁读改写、整体 JSON 快照）
infrastructure/  系统时钟、序列 ID、原子 JSON 快照存储（临时文件 + rename）
interfaces/      WSGI HTTP 边界（http.py）
bootstrap.py     组装根；种子机构/主体/项目/授权
```

时间与标识通过可替换端口接入（测试用 `FixedClock`），运行数据默认写
`./data/`，不写入源码目录。

## 运行

```bash
python3 -m service_09252_010 --host 127.0.0.1 --port 8080 \
    --snapshot ./data/snapshot.json
# 也可用环境变量 EFFECT_SNAPSHOT_PATH 指定快照路径
```

内置演示令牌（仅种子数据）：

| 令牌 | 主体 | 粒度 |
|---|---|---|
| `token-admin` | 管理员 | admin（全部） |
| `token-a` | A国经办机构 | import/read（prj-demo）、signoff |
| `token-b` | B国经办机构 | import/read（prj-demo）、signoff |
| `token-calc` | 计算调度 | calc/read（prj-demo） |
| `token-audit` | 独立复核中心 | review/read（prj-demo） |

## API 一览

所有写接口需 `Authorization: Bearer <token>` 与 JSON 请求体。

### 指标定义
- `POST /metrics` 登记/更新指标（自动递增版本）· `GET /metrics` ·
  `GET /metrics/{code}?version=N`

### 换算规则与口径会签
- `POST /rules` 创建规则版本（`required_parties` 非空则待会签，否则直接生效）
- `GET /rules`
- `POST /rules/{rule_id}/versions/{n}/approve|reject`
- `GET  /rules/{rule_id}/versions/{n}/signoff`
- `POST /rules/{rule_id}/rollback`，body `{"to_version": 1}`

### 数据导入
- `POST /projects/{project_id}/batches`（支持 `idempotency_key`；
  迟到数据自动形成新版本）
- `GET /projects/{project_id}/batches`
- `GET /projects/{project_id}/points?metric=code`

### 计算
- `POST /projects/{project_id}/tasks`，body
  `{"idempotency_key":"...","window_start":"2025-09-01","window_end":"2026-08-31"}`
- `POST /tasks/{id}/run`（并发执行返回 409）
- `POST /tasks/{id}/resume`（断点恢复）
- `GET  /tasks/{id}`
- `GET  /projects/{project_id}/reports?window_start=&window_end=`
- `GET  /projects/{project_id}/reports/diff?window_start=&window_end=`
- `GET  /reports/{id}`

### 复核
- `POST /reports/{id}/reviews`，body `{"state":"approved|rejected","comment":"..."}`
- `GET  /reports/{id}/reviews`

### 导出
- `GET /reports/{id}/export?format=json`（默认；含复算包与指纹）
- `GET /reports/{id}/export?format=csv`

## 缺失值策略

指标可配置 `missing_policy`：
- `exclude`（默认）：缺失数据点不参与；
- `zero`：缺失按 0 计入；
- `fail`：存在缺失则该指标结论为 `indeterminate`（任务仍成功完成，结论不达标）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：缺失值三种策略、跨年度观察窗口、迟到数据版本与报告差异、
指标更新/规则回滚不影响旧报告、并发会签、任务幂等与断点恢复（含重启）、
授权粒度、复核与自包含导出。

## 编译检查

```bash
python3 -m compileall -q service_09252_010 tests
```
