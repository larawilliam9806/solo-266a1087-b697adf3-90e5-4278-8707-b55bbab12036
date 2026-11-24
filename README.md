# 渠道日报换交核对

换交前确认新批次仍能供数的内部核对网页。Flask + SQLite，按月份管理批次：
先登记已验收的首版作为基线，之后上传同月候选批与现行批比对，填入三类偏差
上限，逐渠道核对后决定退回或确认生效。确认后候选批成为该月下游取数的现行批，
核对报告按报告号永久留档。

## 快速启动

### docker compose（推荐）

```bash
docker compose up -d --build
# 打开 http://localhost:8000
```

数据保存在命名卷 `reconcile-data` 的 `/data/reconcile.sqlite3`，容器重启不丢。

### 本地开发

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python wsgi.py          # http://localhost:8000
```

环境变量：`DATABASE_PATH`（SQLite 路径）、`SECRET_KEY`（会话密钥）、
`MAX_CONTENT_LENGTH` 走 Flask 默认配置中的 32 MB 上限。

### 测试

```bash
pip install pytest
python -m pytest -q
```

## 使用流程

1. **登记首版**：导航「登记首版」，选择月份，上传该月已验收的 CSV。
   每个月只允许登记一次首版；已有批次的月份会提示改为上传候选。
2. **上传候选**：进入月份页 →「上传候选批」，上传同月 CSV。
   上传即校验，任一条件不满足整批退回且不入库：
   - 渠道 + 日期必须是唯一键，出现重复键退回；
   - 不得缺少基线中的渠道，缺渠道退回；
   - 任一渠道的金额或转化数整列为空退回；
   - 文件必须只含一个月份，且与当前月份一致。
3. **核对**：在候选批详情页填写三项上限后核对：
   - 键缺失比例上限；
   - 空值率差上限（金额、转化数各自）；
   - 分箱占比差上限（各箱在非空值中的占比差）。

   每个基线渠道都会列出键的缺失比例、金额与转化数的空值率差，以及各箱的
   非空占比差。任一指标的**绝对值**超限即退回，报告逐条说明原因。
4. **确认**：核对通过的报告页点「确认生效」。候选批与报告在同一事务内生效；
   任一步失败则回滚，原批继续供下游取数。
5. **取数**：`/months/<YYYY-MM>/active` 查看、`/months/<YYYY-MM>/active.csv` 下载
   该月现行批。
6. **查结论**：导航「按报告号查询」，或直接访问 `/reports/<报告号>`。

## 统计口径

- **唯一键**：渠道 + 日期。
- **分位点**：非空值升序后取第 `ceil(n × q)` 项（1 起算），
  q ∈ {1/4, 1/2, 3/4}；用整数运算 `(n+3)//4`、`(n+1)//2`、`(3n+3)//4` 避免浮点误差。
- **分箱**：切点相同则合并。切点 c 把数轴切成 `<c`、`=c`、`>c` 等区段；
  **等于切点的值单独成箱**，区间箱只含严格介于两切点之间的值。
- **两版同箱**：基线和候选都使用**基线**的四分位切点分箱，保证占比可比。
- **空值率** = 该渠道该列空值数 / 该渠道键数。
- **箱占比** = 该箱非空值数 / 该列非空值总数。
- **空值率差** = 候选空值率 − 基线空值率；**占比差** = 候选占比 − 基线占比。
  两者均按绝对值与上限比较。

## 数据一致性

- 核对阶段只读候选明细与基线**预存**的键和统计（`batch_keys`、
  `batch_channel_stats`），不写库、不阻塞其他月份的登记。
- 登记批次时即预存键与统计，因此确认后的候选可直接作为下一轮基线。
- 核对完成后若基线被换版，报告标记为「已失效」，确认时提示重算。
- 报告号形如 `R20261124-0001`（UTC 日期 + 当日流水）。

## 目录

```
app/
  __init__.py      Flask 工厂与路由
  service.py       登记、核对、确认、下游取数等业务规则
  stats.py         四分位切点与分箱
  csv_loader.py    CSV 解析与上传校验
  db.py            SQLite 连接、建表、事务
  templates/       页面模板
  static/style.css
tests/test_app.py  规则与流程测试
wsgi.py            应用入口
Dockerfile / docker-compose.yml
```

## 主要路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 月份列表与现行批 |
| GET | `/months/<YYYY-MM>` | 月份详情：批次与报告 |
| GET/POST | `/baseline` | 登记已验收首版 |
| GET/POST | `/months/<YYYY-MM>/candidate` | 上传同月候选批 |
| GET | `/months/<YYYY-MM>/batches/<id>` | 批次明细与核对表单 |
| POST | `/months/<YYYY-MM>/batches/<id>/reconcile` | 执行核对并生成报告 |
| GET | `/reports/<report_no>` | 报告详情 |
| POST | `/reports/<report_no>/confirm` | 确认候选与报告生效 |
| GET | `/reports?report_no=...` | 按报告号查回结论 |
| GET | `/months/<YYYY-MM>/active` | 现行批数据 |
| GET | `/months/<YYYY-MM>/active.csv` | 下载现行批 CSV |
| GET | `/healthz` | 健康检查 |
