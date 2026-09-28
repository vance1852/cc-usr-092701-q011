# 服务接口

所有时间使用带时区的 ISO 8601 格式。服务持久化 UTC 时间，按诊所配置的时区解释运营日期。JSON 请求大小上限为 1 MB；无效请求返回稳定的错误码和 HTTP 状态，不向调用方透出数据库异常。

## 登录与诊所隔离

`POST /auth/token` 接受 `staff_id` 和 `password`，返回有效期不超过一天的 Bearer 凭据。除登录与健康检查外，请求必须同时提供 `Authorization: Bearer …` 和 `X-Clinic-ID`。认证失败不区分账号不存在、停用或密码错误；诊所边界之外的数据返回不存在，避免泄露另一诊所的记录。

`POST /auth/logout` 撤销当前凭据。修改员工密码会撤销该员工的全部活动凭据。初始负责人通过命令行创建；没有可直接注册负责人的 HTTP 路由。

## 患者、评估与诊疗计划

- `POST /patients` 建立诊所内患者档案；外部编号在诊所范围内唯一。
- `GET /patients/{patient_id}` 返回最小档案，不返回联系方式密文。
- `POST /patients/{patient_id}/merge` 以两个版本号和书面原因将重复档案标记为合并，并指向保留档案。
- `POST /patients/{patient_id}/assessments` 新建评估草稿；`POST /assessments/{assessment_id}/sign` 由临床岗位签署。
- `POST /patients/{patient_id}/consents` 创建更高版本的授权；`POST /consents/{consent_id}/withdraw` 撤回授权。
- `POST /patients/{patient_id}/plans` 建立计划，医美和体重管理计划必须引用当前对应授权。
- `POST /plans/{plan_id}/{propose|activate|pause|resume|complete|cancel}` 以 `expected_version` 执行带版本保护的状态转换。
- `GET /patients/{patient_id}/weight-series` 返回按观察时间排序的测量值，不生成诊断或治疗建议。

评估签署后不可覆盖。就诊病历由章节组成，签署需要主诉、评估和计划三部分；签署后的补充内容成为新版本，原始文字仍保留。

## 预约、随访与计划节点

创建预约须提供 `Idempotency-Key`，有责任人的预约不能与未结束时段重叠。临时占位到期后由 `POST /appointments/{id}/book` 拒绝确认，过期占位可通过服务方法按限额释放。预约状态按占位、确认、到诊、服务、完成推进；开始服务时产生就诊记录。

随访和计划节点支持领取租约、版本校验、幂等创建、延期和完整处置历史。旧领取者不能以过期令牌提交结果；重新领取不会删除前次领取事件。

## 诊所耗材

- `POST /products` 登记耗材；`POST /products/{product_id}/lots` 按批号入库。
- `POST /stock/reserve` 依据失效日期按先到期先出分批预留，需要 `Idempotency-Key`。
- `POST /stock/{reservation_id}/consume` 记录患者使用；`release` 释放尚未使用的数量。
- `POST /stock/{lot_id}/quarantine`、`recall` 或 `release-quarantine` 记录批次处置及受影响预留。
- `GET /stock/lots` 查看可用数量；`GET /stock/{lot_id}/history` 查看批次流水。

入库、占用、释放与患者使用均进入不可变流水。存在不足时整笔预留回滚；被隔离、召回或在诊所本地日期已过期的批次不能继续使用。

## 不良事件与数据使用

护理人员可报告事件或患者安全关注项；临床岗位复核并记录处置，诊所负责人可作废就诊记录。`GET /audit/verify` 校验诊所哈希链，`GET /audit/diagnostics` 汇报需人工核对的一致性问题，不自动修改业务状态。

`POST /patients/{patient_id}/export` 只在存在有效数据导出授权时返回明确选择的章节。导出字段采用白名单，联系方式密文、凭据和内部合并字段不会导出；相同幂等请求得到相同内容摘要。`GET /reports/daily`、`appointments`、`incidents` 和 `overdue-milestones` 仅返回运营汇总或经岗位授权的工作队列。

## 跨诊所转诊交接

转诊用于把一位需进一步评估的患者交接给另一家分院，全过程不复制自由病历文本，接收诊所在医生明确接受前不会产生该患者的任何档案。

- `POST /patients/{patient_id}/referrals` 由来源临床岗位发起，需提供 `destination_clinic_id`、`designated_staff_id`、`purpose`（`further_assessment`、`specialty_consult`、`continuity_of_care`、`second_opinion`）、`sections`、`expires_at` 和一份 `referral_disclosure` 专项授权编号。可带 `Idempotency-Key`。
- 专项授权通过 `POST /patients/{patient_id}/consents` 创建，`purpose` 为 `referral_disclosure` 且必须带 `scope` 章节白名单（`profile`、`assessments`、`plans`、`observations`、`clinical_flags`、`encounters`、`incidents`、`followups`）。交接章节必须被授权范围包含。
- 发起即冻结白名单快照：联系方式密文、凭据与内部合并字段永不进入快照；重复发送相同交接（含相同幂等键或相同目的、章节、期限、授权组合）返回原编号与 `replayed: true`。
- `GET /referrals?direction=incoming|outgoing` 查看信封列表；医生只看到指定给自己的入站交接，诊所负责人可查看全所队列。
- `GET /referrals/{id}`：来源侧看交接状态；接收侧在 `pending` 或 `declined` 状态只看到信封，`accepted` 后才可读取授权范围内的快照章节。每次读取都写入双侧访问审计。
- `POST /referrals/{id}/accept` 或 `/decline`（后者必须给 `decline_reason`）由被指定医生（或接收诊所负责人）凭 `expected_version` 答复。接受不创建接收诊所患者档案；接受前任何章节均不可读。
- 快照固化在发起时点。来源记录随后变化时，`GET` 仅返回 `source_changed: true` 提示，不静默返回新内容；来源侧 `POST /referrals/{id}/refresh` 重新冻结最新数据并生成**新编号**，旧交接置为 `superseded`。
- 患者撤回 `referral_disclosure` 授权、授权到期或交接过期后，交接立即封存（`sealed`）：从未被读取的章节从快照中删除且不可继续访问，已读取章节保留；全部访问与封存审计在两家诊所的哈希链中均保留。`POST /referrals/expire` 可批量清扫到期交接。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
- 转诊交接：待答复 → 已接受或已拒绝；来源侧重新获取时旧交接变为已取代。撤回授权或到期会封存交接（独立于答复状态），封存后未读章节不可访问。
