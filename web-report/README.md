# 手机交互报告

这是原Tommy-Capital-AI仓库的网页入口，复用已发布的正式中文报告、机器摘要、累计Top10统计与Python MACD监控结果；不运行第二套选股或背离算法。

网站：https://tommy-capital-report.stableokapi18.chatgpt.site

网站设为所有者私有，用创建该网站的同一个ChatGPT账号登录。手机使用5G或Wi-Fi均可，不依赖电脑开机。GitHub报告默认链接到这个HTTPS地址；原Windows `Start-Report.cmd`保留为本机完整监控程序。

页面包括最新报告、观察与实际持仓。忽略/恢复、手动观察、持仓新增/编辑/删除存于Sites D1私有数据库，以平台验证的用户ID隔离；不上传GitHub，不写进公开报告，不自动改变云端忽略设置或电脑文件。更新使用revision比较，冲突时拒绝覆盖。可下载私人名单备份。

MACD表只显示原Python引擎在公开收盘报告中发布的结果，显示原始检查时间、周期行情时间及来源。刷新网页仅拉取最新已发布报告，不触发全市场重扫。新增私人股票若不在本次公开监控范围，显示“未有已发布结果/等待独立监控”，完整五周期即时刷新仍使用电脑窗口；不能把保存名单冒充已完成行情抓取。网页不保存或重新计算真实持仓的历史最大浮盈。

优先通过公开Git refs固定提交，读取Markdown、JSON、忽略文件与扫描状态，不依赖GitHub共享REST读取配额。若refs不可用，改为读取公开raw文件并双读机器摘要、核对扫描起止时钟和当天closing归档，拒绝混合缓存；界面显示校验值而不虚构提交号。验证收盘类型、交易日、分时截止、最多20只和报告日期，保留partial/scan_complete=false，不把模型复核或失败诊断伪装成收盘行情。

## 检查与部署

- Node.js 24：`node --test tests/*.test.mjs`，测试不需要访问实时行情。
- 依赖安装后：`node node_modules/typescript/bin/tsc --noEmit`。
- 按Sites提供的构建与发布流程构建Vinext/Worker。`.openai/hosting.json`保存既有项目ID及D1绑定`DB`，不要新建替代项目。
- 数据库迁移使用`drizzle/`里的迁移文件；不在请求处理中创建或修改表。
- 私有访问令牌只由Sites平台处理，禁止写进源码、浏览器包、README或GitHub Actions日志。

电脑端与网页端的私人名单目前各自保存；云端跨设备忽略使用报告中的原GitHub观察名单工作流入口。
