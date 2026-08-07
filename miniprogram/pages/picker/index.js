const api = require('../../utils/api')

Page({
  data: {
    loading: true,
    groups: [],
    maxTopics: 3,
    selected: [], // topic key 数组，顺序即展示顺序
    labels: {}, // key -> 中文名，用于底部已选栏
    saving: false,
    isEdit: false, // 从内容页进来是「修改」，启动进来是「首次选择」
  },

  onLoad(query) {
    this.setData({ isEdit: query.edit === '1' })
    wx.setNavigationBarTitle({
      title: query.edit === '1' ? '修改方向' : '选择你关心的方向',
    })
    this.load()
  },

  // 从自定义页返回时会触发，需要把新建的 topic 并进来
  onShow() {
    const pending = getApp().globalData.pendingCustomTopic
    if (!pending) return
    getApp().globalData.pendingCustomTopic = null
    this.addTopic(pending.key, pending.label)
  },

  async load() {
    try {
      const [cat, sess] = await Promise.all([api.catalog(), api.session()])
      const labels = {}
      cat.groups.forEach((g) =>
        g.topics.forEach((t) => {
          labels[t.key] = t.label
        })
      )
      // 老用户带着已选进来；自定义 topic 不在 catalog 里，标签先用 key 兜底
      const selected = sess.topics || []
      selected.forEach((k) => {
        if (!labels[k]) labels[k] = '自定义方向'
      })
      this.setData({
        groups: cat.groups,
        maxTopics: cat.max_topics || 3,
        selected,
        labels,
        loading: false,
      })
    } catch (e) {
      api.toastError(e)
      this.setData({ loading: false })
    }
  },

  onTapTopic(e) {
    const { key } = e.currentTarget.dataset
    this.addTopic(key, this.data.labels[key])
  },

  addTopic(key, label) {
    const selected = this.data.selected.slice()
    const labels = Object.assign({}, this.data.labels)
    if (label) labels[key] = label

    const i = selected.indexOf(key)
    if (i >= 0) {
      selected.splice(i, 1) // 再点一次取消
    } else {
      if (selected.length >= this.data.maxTopics) {
        wx.showToast({
          title: `最多选 ${this.data.maxTopics} 个，先取消一个`,
          icon: 'none',
        })
        return
      }
      selected.push(key)
      wx.vibrateShort({ type: 'light' })
    }
    this.setData({ selected, labels })
  },

  onTapCustom() {
    if (this.data.selected.length >= this.data.maxTopics) {
      wx.showToast({ title: '已经选满了', icon: 'none' })
      return
    }
    wx.navigateTo({ url: '/pages/custom/index' })
  },

  async onSave() {
    const { selected, saving } = this.data
    if (!selected.length || saving) return
    this.setData({ saving: true })
    try {
      await api.setTopics(selected)
      getApp().globalData.session = null // 让内容页重新拉
      wx.reLaunch({ url: '/pages/digest/index' })
    } catch (e) {
      // 服务端是最终裁判：本地算漏了它也会拦住
      api.toastError(e)
      this.setData({ saving: false })
    }
  },
})
