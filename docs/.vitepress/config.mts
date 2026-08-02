import { defineConfig } from 'vitepress'

const repo = 'https://github.com/Eco404/astrbot_plugin_livingmemory'

export default defineConfig({
  title: 'LivingMemory',
  description: 'Source-grounded Timeline and Topic memory for AstrBot',
  base: process.env.DOCS_BASE || '/astrbot_plugin_livingmemory/',
  cleanUrls: true,
  lastUpdated: true,
  ignoreDeadLinks: false,
  head: [
    ['meta', { name: 'theme-color', content: '#187b78' }],
    ['meta', { name: 'color-scheme', content: 'light dark' }],
  ],
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      title: 'LivingMemory',
      description: '为 AstrBot 构建可溯源、可维护的长期记忆',
      themeConfig: localeTheme({
        nav: navZh(),
        sidebar: sidebarZh(),
        outline: '本页目录',
        lastUpdated: '最后更新',
        prev: '上一页',
        next: '下一页',
        edit: '在 GitHub 上编辑此页',
        pathPrefix: 'docs',
      }),
    },
    en: {
      label: 'English',
      lang: 'en-US',
      title: 'LivingMemory',
      description: 'Source-grounded, maintainable long-term memory for AstrBot',
      themeConfig: localeTheme({
        nav: navEn(),
        sidebar: sidebarEn(),
        outline: 'On this page',
        lastUpdated: 'Last updated',
        prev: 'Previous',
        next: 'Next',
        edit: 'Edit this page on GitHub',
        pathPrefix: 'docs',
      }),
    },
  },
  themeConfig: {
    logo: '/logo.png',
    siteTitle: 'LivingMemory',
    socialLinks: [{ icon: 'github', link: repo }],
    search: { provider: 'local' },
  },
})

function localeTheme(options: {
  nav: any[]
  sidebar: any[]
  outline: string
  lastUpdated: string
  prev: string
  next: string
  edit: string
  pathPrefix: string
}) {
  return {
    nav: options.nav,
    sidebar: options.sidebar,
    outline: { label: options.outline, level: [2, 3] as [number, number] },
    lastUpdated: {
      text: options.lastUpdated,
      formatOptions: { dateStyle: 'medium', timeStyle: 'short' },
    },
    docFooter: { prev: options.prev, next: options.next },
    editLink: {
      pattern: `${repo}/edit/master/${options.pathPrefix}/:path`,
      text: options.edit,
    },
  }
}

function navZh() {
  return [
    { text: '开始使用', link: '/guide/getting-started' },
    { text: '记忆架构', link: '/architecture' },
    { text: '召回', link: '/recall' },
    { text: '维护', link: '/maintenance' },
    { text: 'GitHub', link: repo },
  ]
}

function navEn() {
  return [
    { text: 'Get started', link: '/en/guide/getting-started' },
    { text: 'Architecture', link: '/en/architecture' },
    { text: 'Recall', link: '/en/recall' },
    { text: 'Maintenance', link: '/en/maintenance' },
    { text: 'GitHub', link: repo },
  ]
}

function sidebarZh() {
  return [
    {
      text: '开始使用',
      items: [
        { text: '快速开始', link: '/guide/getting-started' },
        { text: 'WebUI 导览', link: '/webui' },
        { text: '配置参考', link: '/configuration' },
      ],
    },
    {
      text: '记忆系统',
      items: [
        { text: '整体架构', link: '/architecture' },
        { text: 'Timeline 记忆', link: '/timeline-memory' },
        { text: 'Topic 记忆', link: '/topic-memory' },
        { text: '召回与注入', link: '/recall' },
      ],
    },
    {
      text: '运维与数据',
      items: [
        { text: '维护中心', link: '/maintenance' },
        { text: '数据安全与迁移', link: '/data-safety' },
        { text: '详细开发日志', link: '/DEVELOPMENT_LOG' },
        { text: '命令速查', link: '/commands' },
      ],
    },
  ]
}

function sidebarEn() {
  return [
    {
      text: 'Get started',
      items: [
        { text: 'Quick start', link: '/en/guide/getting-started' },
        { text: 'WebUI tour', link: '/en/webui' },
        { text: 'Configuration', link: '/en/configuration' },
      ],
    },
    {
      text: 'Memory system',
      items: [
        { text: 'Architecture', link: '/en/architecture' },
        { text: 'Timeline memory', link: '/en/timeline-memory' },
        { text: 'Topic memory', link: '/en/topic-memory' },
        { text: 'Recall and injection', link: '/en/recall' },
      ],
    },
    {
      text: 'Operations and data',
      items: [
        { text: 'Maintenance center', link: '/en/maintenance' },
        { text: 'Data safety and migration', link: '/en/data-safety' },
        { text: 'Command reference', link: '/en/commands' },
      ],
    },
  ]
}
