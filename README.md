# KiliKili Backend

给 **KiliKili**（[jiehuan-jason/KiliKili](https://github.com/jiehuan-jason/KiliKili)）J2ME 客户端用的 **B 站后端代理**。

原客户端只带前端，后端（`/search` `/view` `/list` `/playurl` `/download` 等服务）需要自建。这个项目就是那套后端 —— 让 **2008 年的 S60v3 手机**（如诺基亚 E63）能搜索、浏览、下载并播放 B 站视频。

> 实测环境：诺基亚 E63（Symbian 9.2 / CLDC 1.1 / MIDP 2.0 / 仅移动数据），
> 下载 18 分钟视频（12 MB）并成功播放。

## 架构

```
E63 (J2ME MIDlet)
  │ 纯 HTTP（不支持 TLS 1.2，所以必须明文）
  ▼
固定域名入口（serveo 固定子域名 / 任意可访问的 HTTP 地址）
  ├─ /search /view /list /rcmd     小响应，走这里
  └─ /playurl → 返回【当前视频流地址】
                     ▲
              可选：host-watcher 监听隧道域名变化
  ▼
视频流隧道（cpolar / 任何带宽足够的 HTTP 通道）
  ▼
本服务 :8083  →  B 站 API + ffmpeg 转码
```

**为什么要分两条隧道**：免费跨境隧道（serveo/localhost.run）实测约 16 KB/s 且单连接 **20 秒 / 200 KB 后被强制切断** —— API 小响应没问题，视频文件永远下不完。cpolar 免费版实测 35~54 KB/s 且无切断，所以视频走 cpolar。

**为什么客户端不用改**：客户端只认一个固定地址；视频地址由服务端动态返回（`current_host.txt`），隧道换域名时客户端无感。

## 端点

| 端口/路径 | 说明 | 返回 |
|---|---|---|
| `GET /search?keyword=&page=` | 搜索视频 | B 站 `search/type` 原始 JSON |
| `GET /view?bvid=` | 视频信息 | B 站 `web-interface/view` 原始 JSON |
| `GET /list?bvid=` | 分 P 列表 | B 站 `player/pagelist` |
| `GET /user?mid=` | 用户信息 | B 站 `web-interface/card` |
| `GET /user/video?mid=` | 用户投稿 | B 站 `space/wbi/arc/search` |
| `GET /rcmd` | 推荐 | B 站 `wbi/index/top/feed/rcmd` |
| `GET /playurl?bvid=&cid=` | 播放地址 | `{"code":0,"url":"http://<stream-host>/stream?..."}` |
| `GET /stream?bvid=&cid=` | 视频流（无转码产物则先转码） | `video/mp4` |
| `GET /download?bvid=&cid=` | 触发转码 | `{"code":0,"taskId":...}` |
| `GET /status?taskId=` | 转码状态 | `{"code":0,"status":"running|done|error"}` |
| `GET /output/<id>_240p.mp4` | 转码产物 | `video/mp4` |
| `GET /ping` | 健康检查 | `kilikili backend OK` |

客户端解析很朴素（找 `"key"` 后取引号内容），所以直接透传 B 站原始 JSON 即可，只要保证 `code` 为 0/1。

## 运行

```bash
# 依赖：python3（仅标准库）+ ffmpeg
python3 server.py single 8083     # 单端口模式（推荐，配合一条隧道）
python3 server.py                 # 四端口模式 3000/2121/3232/4000（局域网调试用）
```

### 转码参数

为低带宽定制，可在 `transcode_worker()` 里调：

```
256x144 · 12fps · H.264 Constrained Baseline · 72 kbps
AAC-LC 24 kbps 单声道 22.05 kHz
→ 合计约 98.6 kbps ≈ 12.3 KB/s
```

带宽充裕时可以调到 `scale=480:270` / `-b:v 200k`，画质明显更好。

## 客户端编译（KiliKili.jar）

原仓库只有源码，需要自己编成 J2ME midlet：

1. **必须用 JDK 8**：`javac -source 1.4 -target 1.4`
   （CLDC 1.1 没有 `java.lang.StringBuilder`，新版 javac 会生成它，导致设备上 `NoClassDefFoundError`）
2. CLDC/MIDP API stubs（Maven）：
   `org.microemu:cldcapi11:2.0.4` + `org.microemu:midpapi20:2.0.4`
3. 修改 `URLget.java` 的服务器地址：
   ```java
   public static String IP_ADDRESS="your-host.example.com";
   ```
4. 打包 + **预校验**（MIDP 必须）：
   ```
   javac -source 1.4 -target 1.4 -bootclasspath cldcapi11.jar:midpapi20.jar -d classes src/**.java
   jar cfm KiliKili-raw.jar manifest.txt -C classes .
   java -jar proguard.jar @proguard.pro      # -microedition 生成 StackMap 属性
   ```
5. 生成 `.jad`（`MIDlet-Jar-Size` 必须等于 jar 字节数），与 jar 一起放进手机存储卡安装。

## 已验证 / 已知限制

**可用**
- 搜索、视频详情、分 P、推荐、用户信息
- 视频下载与播放（256×144，约 12 KB/s 码率）
- B 站搜索反爬（无 Cookie 返回 HTTP 412）→ 自动取 `buvid3` 并重试

**限制**
- 免费隧道带宽低，长视频仍需等待
- 某些隧道服务会切断 >20 秒 / 200 KB 的连接（serveo 实测），务必先测通道再选
- 樱花 FRP 明文 HTTP 被合规拦截（返回 501），不可用
- cpolar 免费版不支持固定子域名，重启换域名 → 用 `host-watcher` 跟随
- 客户端部分功能（登录、评论）原作者未实现

## License

MIT
