// 后端调用封装。
//
// 走 wx.cloud.callContainer 而不是 wx.request：云托管用微信自己的协议转发，
// 不需要备案域名、不需要配请求白名单，而且会自动注入 X-WX-OPENID，
// 后端拿到的就是当前用户，前端不需要做 wx.login 换 openid 那一步。
//
// DEV_BASE_URL 填了就退回 wx.request，用于在电脑上跑 python api.py 联调。
// 那条路径没有 X-WX-OPENID，所以手动带一个假的，仅限开发。

const { CLOUD_ENV, SERVICE_NAME, DEV_BASE_URL } = require('./config')

const DEV_OPENID = 'dev_local_user'

function normalize(res) {
  const body = res.data
  if (res.statusCode >= 400 || !body || body.ok === false) {
    const msg = (body && body.error) || `请求失败 (${res.statusCode})`
    const err = new Error(msg)
    err.statusCode = res.statusCode
    throw err
  }
  return body
}

function call(path, { method = 'POST', data = {} } = {}) {
  // GET 的参数要拼进 query，callContainer 不会帮你做这件事
  let url = path
  if (method === 'GET' && Object.keys(data).length) {
    const qs = Object.keys(data)
      .filter((k) => data[k] !== undefined && data[k] !== null)
      .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(data[k])}`)
      .join('&')
    if (qs) url = `${path}?${qs}`
  }

  return new Promise((resolve, reject) => {
    const onDone = (res) => {
      try {
        resolve(normalize(res))
      } catch (e) {
        reject(e)
      }
    }
    const onFail = (err) =>
      reject(new Error(err.errMsg || '网络异常，请稍后再试'))

    if (DEV_BASE_URL) {
      wx.request({
        url: DEV_BASE_URL + url,
        method,
        data: method === 'GET' ? {} : data,
        header: {
          'content-type': 'application/json',
          'X-WX-OPENID': DEV_OPENID,
        },
        success: onDone,
        fail: onFail,
      })
      return
    }

    wx.cloud.callContainer({
      config: { env: CLOUD_ENV },
      path: url,
      method,
      data: method === 'GET' ? {} : data,
      header: {
        'X-WX-SERVICE': SERVICE_NAME,
        'content-type': 'application/json',
      },
      success: onDone,
      fail: onFail,
    })
  })
}

// 出错时统一弹提示，页面只管 catch 里调它
function toastError(e) {
  wx.showToast({
    title: (e && e.message) || '出错了',
    icon: 'none',
    duration: 2500,
  })
}

module.exports = {
  call,
  toastError,

  session: () => call('/api/session'),
  catalog: () => call('/api/catalog', { method: 'GET' }),
  setTopics: (topics) => call('/api/topics', { data: { topics } }),
  createCustom: (description) =>
    call('/api/topics/custom', { data: { description } }),
  digest: (day) => call('/api/digest', { method: 'GET', data: { day } }),
  setActive: (active) => call('/api/unsubscribe', { data: { active } }),
}
