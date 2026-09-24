# 封锁窗口调度辅助（模拟）

模拟"正在执行的封锁窗口被要求提前结束、同时换班"的调度辅助场景。
**系统只辅助记录和检查，不自行下达设备操作指令**——所有收尾建议均为
待负责人确认的记录，执行主体始终是现场人员。

## 核心规则与实现位置

| 规则 | 实现 |
| --- | --- |
| 可中断点、撤离所需时间作为计划配置 | `models.py` 的 `StepExitConfig` / `InterruptiblePoint` |
| 窗口压缩时逐步骤评估退出条件，生成待负责人确认的收尾建议 | `coordinator.py` 的 `request_early_end` / `_evaluate` |
| 时间不足以满足退出条件 → 明确的异常协调状态 | `PlanState.EXCEPTION_COORDINATION`，步骤置为 `CANNOT_SAFELY_EXIT` |
| 不能把步骤强制改成完成 | `complete_step` 对 `CANNOT_SAFELY_EXIT` 一律拒绝；无批量强制完成入口 |
| 新班确认接收前旧班不得交出责任 | `handover.py` 的 `ShiftHandover.release` |
| 交接未闭合禁止完成销记 | `coordinator.py` 的 `sign_off`（另校验异常协调、步骤闭合、建议确认） |
| 交班确认迟到不生效 | `LateHandoverConfirmation`，交接保持 `OFFERED` |
| 许可变更版本严格递增 | `permits.py` 的 `PermitRegistry.issue/revoke` |
| 过期确认不得恢复已失效的开工资格 | `is_work_eligible` 只认可当前版本且未过期的确认；旧版本确认抛 `StalePermitVersion` |
| 窗口压缩后恢复 | `restore_window`：重新评估，可恢复的步骤解除阻断，旧建议标记 `SUPERSEDED` |

## 目录结构

```
maintenance_window/
  clock.py        # 手动时钟，便于模拟迟到/过期
  errors.py       # 领域规则错误
  models.py       # 窗口、计划、步骤、收尾建议、审计事件
  permits.py      # 许可与保护确认（递增版本）
  handover.py     # 交接班状态机
  coordinator.py  # 调度协调器（核心规则）
tests/
  test_permits.py            # 许可版本与开工资格
  test_handover.py           # 交接班
  test_early_end.py          # 压缩/恢复/异常协调/销记守卫
  test_combined_scenario.py  # 组合场景：压缩→恢复 + 确认迟到 + 部分撤销
```

## 运行测试

```bash
cd /workspace && python3 -m unittest discover -v
```
