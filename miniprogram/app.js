const api = require('./utils/api')

App({
  globalData: {
    session: null, // 最近一次 /api/session 的结果，页面间共享
  },

  onLaunch() {
    if (!wx.cloud) {
      console.error('基础库版本过低，请在 project.config.json 里提高 libVersion')
      return
    }
    wx.cloud.init({ traceUser: true })
  },

  // 启动分流：新用户去选题，老用户直接看内容。
  // 放在这里而不是 onLaunch，是因为要等页面栈就绪才能 reLaunch。
  async routeOnStart(fallbackPage) {
    try {
      const s = await api.session()
      this.globalData.session = s
      const target = s.new_user ? '/pages/picker/index' : '/pages/digest/index'
      if (fallbackPage !== target) wx.reLaunch({ url: target })
      return s
    } catch (e) {
      api.toastError(e)
      return null
    }
  },
})
