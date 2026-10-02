# 跨境班列箱货节点与异常责任

跟踪中欧班列箱货编组、口岸节点、异常处置与交付承诺。

## 参与方与事实

主要参与方包括班列运营方、货代、铁路承运方、口岸查验人员、目的站交接人员。领域资料记录以下已经确认的事实：

- 南昌国际陆港开行直达比什凯克的中欧班列
- 列车装载太阳能组件、陶瓷餐具和灯具
- 线路经霍尔果斯口岸出境并直达中亚商贸枢纽

## 业务约束

- 箱货身份
- 节点顺序
- 单证一致
- 局部冻结
- 交付承诺

五条约束在 `docs/domain-rules.md` 中细化为履约规则。

## 履约服务

`src/journey.py` 是纯内存领域模型：

- `Order` / `ShipmentUnit`：为每个客户订单保存箱号、货类、重量、单证、
  班列（铁路区段）、装载计划、节点回执与责任主体；
- `Receipt` + `JourneyService.apply_receipt`：货代/铁路/口岸/目的站回执
  按固定节点链推进；重复回执不重复推进，重量或单证变化进入核对；
- `freeze_unit` / `resolve_freeze`：危险属性与查验异常只冻结受影响箱货，
  整列其他箱子继续运行；
- `split_unit` / `merge_units` / `reassign_to_next_train`：
  拆分、合并、换班（改走下一班）以谱系事件串联；
- `record_delay`：延误顺延预计交付区间；
- `explain_order`：按客户订单解释当前去向、下一责任人、
  预计交付区间与承诺是否受影响。

`src/store.py` 在领域模型外提供事件日志（JSONL）：命令先校验后落事件，
重启重放即可恢复全部状态，延误、改配和异常处置跨越停机继续推进。
入口为 `open_app(path)`，返回 `AppService`。

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/rail_context.py` 负责读取和校验这些资料。

## 开发命令

运行测试（含首趟南昌—比什凯克班列的完整场景：单箱暂扣、拆分、
改走下一班、重复回执、核对、延误与重启恢复）：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

两条命令只读写仓库内文件（履约测试使用临时目录中的事件日志），
不需要连接外部业务系统。
