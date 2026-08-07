const api = require('../../utils/api')

Page({
  data: {
    loading: true,
    digest: null,
    reason: '',
    dateText: '',
    failed: false,
  },

  onLoad() {
    this.boot()
  },

  // 从选题页保存后回来会走这里，需要重新拉一次
  onShow() {
    if (!this.data.loading && !getApp().globalData.session) this.load()
  },

  onPullDownRefresh() {
    this.load().then(() => wx.stopPullDownRefresh())
  },

  async boot() {
    // 首次进入先确认身份：没选过方向的人不该看到空白内容页
    const app = getApp()
    const s = await app.routeOnStart('/pages/digest/index')
    if (!s) {
      this.setData({ loading: false, failed: true })
      return
    }
    if (s.new_user) return // routeOnStart 已经跳走了
    this.load()
  },

  async load() {
    this.setData({ failed: false })
    try {
      const r = await api.digest()
      getApp().globalData.session = getApp().globalData.session || {}
      this.setData({
        digest: r.digest,
        reason: r.reason || '',
        dateText: r.digest ? this.formatDate(r.digest.date) : '',
        loading: false,
      })
    } catch (e) {
      api.toastError(e)
      this.setData({ loading: false, failed: true })
    }
  },

  formatDate(iso) {
    const d = new Date(iso.replace(/-/g, '/'))
    const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()]
    return `${d.getMonth() + 1}月${d.getDate()}日 ${week}`
  },

  // 小程序打不开外部链接：web-view 只能加载已配置业务域名的页面，
  // 而这些 url 是新闻站/GitHub/arXiv，配不了。复制到剪贴板是唯一可行的出路。
  onTapItem(e) {
    const { url, title } = e.currentTarget.dataset
    if (!url) return
    wx.setClipboardData({
      data: url,
      success: () =>
        wx.showToast({ title: '链接已复制，去浏览器打开', icon: 'none', duration: 2000 }),
    })
  },

  onEdit() {
    wx.navigateTo({ url: '/pages/picker/index?edit=1' })
  },

  onShareAppMessage() {
    return {
      title: '挑三个你关心的方向，每天一份简短摘要',
      path: '/pages/digest/index',
    }
  },

  onShareTimeline() {
    return { title: '挑三个你关心的方向，每天一份简短摘要' }
  },
})
