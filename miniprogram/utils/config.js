// 填这两个就能跑。
//
// CLOUD_ENV   微信云托管环境 ID。云托管控制台 → 环境 → 环境 ID。
// SERVICE_NAME 云托管里的服务名，跟你部署时填的一致（Dockerfile 部署那个）。
//
// 这两个不是密钥，可以提交到仓库。真正的密钥（OPENROUTER_API_KEY 等）
// 只存在于云托管的环境变量里，前端永远拿不到。
module.exports = {
  CLOUD_ENV: 'prod-xxxxxxxx',
  SERVICE_NAME: 'open-digest',

  // 本地调试用：填了就走这个地址，绕开云托管，方便在电脑上跑 python api.py 联调。
  // 上线前务必清空——开发者工具需要勾「不校验合法域名」才能用。
  DEV_BASE_URL: '',
}
