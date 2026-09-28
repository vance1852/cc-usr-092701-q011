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

转诊交接以患者的**专项披露授权**（授权用途 `referral_disclosure`）为依据，把授权范围内的章节快照交给另一家诊所，而不是复制病历原文：

- `POST /referrals`（需 `Idempotency-Key`）由来源诊所临床岗位发起，须指定 `destination_clinic_id`、`patient_id`、`purpose`、`sections`、`consent_id` 与 `expires_at`。交接有效期必须晚于当前时间且不晚于专项授权到期时间；章节取自与导出相同的白名单，联系方式密文等内部字段不会进入快照。
- `GET /referrals/incoming`、`GET /referrals/outgoing` 由来源/接收诊所分别查看本侧交接清单，只返回元数据（含 `snapshot_digest` 与 `snapshot_available`），不返回快照正文。
- `POST /referrals/{id}/respond` 由接收诊所医生或负责人提交 `decision=accept|decline`（带 `expected_version`）。拒绝须提供 `reason`；接受时可指定 `assignee_id`，未指定则为操作者本人。**接受前接收诊所不存在该患者的任何普通档案**；接受瞬间才建立在诊档案（`external_ref` 形如 `ref:{referral_id}`）。
- `GET /referrals/{id}` 查看交接元数据；`GET /referrals/{id}/accesses` 查看该交接的逐次访问流水。
- `GET /referrals/{id}/snapshot` 只在接受后、且仅由指定接收医生（或本所负责人）读取授权章节，可用 `?sections=` 进一步取子集，但不能超出授权章节。
- `POST /referrals/{id}/revoke` 由来源诊所在患者撤回授权或要求停止披露时吊销。

交接快照在发送时固化。之后来源记录变化不会静默扩大披露：读取快照时逐章节与来源现状比对，返回 `source_changed` 与 `changed_sections`，并在 `notice` 中提示由来源重新发起交接。患者撤回专项授权（`POST /consents/{id}/withdraw`，或签署更新版本替代旧授权）或交接过期后，快照内容立即销毁且不能继续访问；已接受交接在到期/撤回时惰性或经 `referrals.expire_due` 批量落入终态。无论终态如何，`referral_accesses` 逐次访问流水和来源、接收两侧哈希链审计均永久保留。重复发起相同交接（相同来源诊所与幂等编号、相同内容）返回原编号与原 `snapshot_digest`；相同编号但内容不同返回冲突。

交接状态：待回应 → 已接受 / 已拒绝；待回应或已接受 → 已吊销 / 已过期。已拒绝、已吊销、已过期为终态，终态下快照正文不可保留、不可读取。

## 主要状态

- 计划：草稿 → 提议 → 生效；可暂停和恢复，完成或取消后不能重新激活。
- 预约：占位 → 确认 → 到诊 → 服务中 → 完成；取消和未到诊是独立终态。
- 不良事件：已报告 → 分诊 → 观察 → 已解决 → 关闭。每次处置单独记录操作人和理由。
- 耗材预留：预留 → 释放或核销。库存数量由收货、预留、释放和更正流水求和，不直接改写历史数量。
