# 小程序前端

WXML + WXSS + JS，不是 HTML。标签是 `<view>` `<text>`，样式单位是 `rpx`（屏幕宽固定
750rpx，不用管机型），逻辑写在 `Page({})` 里。

```
app.js / app.json / app.wxss     全局：云环境初始化、页面注册、通用样式
utils/config.js                  ← 只有这个文件需要你改
utils/api.js                     后端调用封装（callContainer）
pages/digest/                    每日内容，也是首页
pages/picker/                    选方向，最多 3 个
pages/custom/                    自己描述一个方向
```

## 跑起来

1. 微信开发者工具 → 导入项目 → 选 `miniprogram/` 目录，填你的 AppID
2. 改 `utils/config.js`：

   ```js
   CLOUD_ENV: 'prod-xxxxxxxx',   // 云托管控制台 → 环境 → 环境 ID
   SERVICE_NAME: 'open-digest',  // 部署时填的服务名
   ```

3. `project.config.json` 里的 `appid` 换成你自己的

### 先在电脑上联调

不想每次都部署到云托管的话：

```bash
python api.py            # 本地 8080
```

`utils/config.js` 里把 `DEV_BASE_URL` 填成 `http://127.0.0.1:8080`，开发者工具里勾上
「不校验合法域名」。这条路径没有 `X-WX-OPENID`，`utils/api.js` 会带一个固定的假 openid
（`dev_local_user`），方便反复测。**上线前记得清空 `DEV_BASE_URL`。**

## 三个页面

**digest（首页）** 启动时调 `/api/session`：没选过方向的人被 `reLaunch` 到选题页，
选过的直接看内容。下拉刷新，空态区分「今天还没生成」和「连不上」。

**picker** 分组展示预设，点一下选中再点取消，选满 3 个再点会提示。底部实时显示
`2/3` 和已选名称。前端拦一遍上限只是体验，**服务端才是最终裁判** —— 前端算漏了
`/api/topics` 也会返回 `at most 3 topics`。

**custom** 输入一句话 → `/api/topics/custom` → 展示后端理解出来的 focus 和数据源 →
用户确认。创建和订阅是分开的两步，中途退出不会留下任何订阅。

## 为什么原文链接是「复制」不是「打开」

小程序不能直接打开外部网页。`<web-view>` 只能加载配置过**业务域名**的页面，而业务域名
必须是你自己的、已备案的域名。摘要里的链接是新闻站、GitHub、arXiv，你配不了。

所以点击条目走 `wx.setClipboardData`，提示「链接已复制，去浏览器打开」。资讯类小程序
基本都这么做。别试图绕，`web-view` 加载未配置域名会直接白屏。

## 改动时注意

`test_miniprogram.py`（在仓库根目录跑）会静态检查：

- 所有 JSON 能解析、`app.json` 里的页面文件都在
- WXML 里的 `bindtap` 都能在 JS 里找到对应函数
- 没混用 HTML 标签
- **前端读的字段后端确实会返回** —— 它会解析 `api.py` 和 `service.py`，
  把接口返回的 key 和 JS/WXML 里读的对一遍

最后这条是最有用的。小程序没有无头运行时，跑不了 UI，但字段对不上是最费时间的那类
bug，静态就能抓出来。加了新接口或改了返回结构，跑一下它。
