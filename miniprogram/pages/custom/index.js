const api = require('../../utils/api')

// 创建和订阅是分开的两步：这里只创建 + 预览，用户确认后回选题页才真正订阅。
// 中途退出不会留下任何订阅。
Page({
  data: {
    desc: '',
    building: false,
    topic: null, // 后端返回的 {key, label, focus, preview}
  },

  onInput(e) {
    this.setData({ desc: e.detail.value })
  },

  onPickExample(e) {
    this.setData({ desc: e.currentTarget.dataset.text })
  },

  async onBuild() {
    const desc = this.data.desc.trim()
    if (desc.length < 2) {
      wx.showToast({ title: '再多写几个字', icon: 'none' })
      return
    }
    this.setData({ building: true, topic: null })
    try {
      const r = await api.createCustom(desc)
      this.setData({ topic: r.topic })
    } catch (e) {
      // 429 是频率限制，422 是内容审核没过，都直接把后端的话显示出来
      api.toastError(e)
    } finally {
      this.setData({ building: false })
    }
  },

  onConfirm() {
    const { topic } = this.data
    if (!topic) return
    // 通过 globalData 回传，选题页 onShow 时取走
    getApp().globalData.pendingCustomTopic = topic
    wx.navigateBack()
  },

  onRetry() {
    this.setData({ topic: null })
  },
})
