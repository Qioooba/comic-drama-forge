# H3 模板测试案例备份

这两个文件是 P1-6 清理前的**完整生产模板原件**，仅用于恢复与回归测试，**不得直接作为正式工作流提交**。

- `h3_director_r2v_单采.pre_clean.json`：含 seed=1001、43 帧空 timeline。
- `minimax_h3_director_二采_加速.pre_clean.json`：含猴子跑动/穿衣/上学测试案例。

正式任务由 `h3_director_builder` 注入真实 timeline；`template_test_guard` 会在最终提交前拦截这些测试数据。
