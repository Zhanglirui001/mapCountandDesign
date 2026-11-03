# Changelog

## 1.7.10

### Fixes
- fix: 添加内部主线铜铝转接阈值

## 1.7.9

### Fixes
- fix: 为Cluster类型多加一对标签

## 1.7.8

### Fixes
- fix: 出图程序表添加新字段

## 1.7.7

### Fixes
- fix: LYNX Plus产品主线上的标签
- fix: 添加Box Position负值校验

## 1.7.6

### Fixes
- fix: 修改Dust cap

## 1.7.5

### Fixes
- fix: 修复homerun导线材料问题
- fix: 合并extender产品
- fix: 产品合并要求preset相同

## 1.7.4

### Fixes
- fix: 修复homerun extender的长度计算错误

## 1.7.3

### Fixes
- fix: 按未合并逻辑生成homerun extender

## 1.7.2

### Fixes
- fix: 修复连接器公母头方向

## 1.7.1

### Fixes
- fix: 修复外部主线连接器使用错误的问题

## 1.7.0

### Features
- feat: 添加末端支线线号和保险丝规格设定字段

### Fixes
- fix: 修复gap extender两端均使用公头连接器的问题
- fix: 修复拖动排序与历史记录回退的兼容性问题

## 1.6.2

### Fixes
- fix: 添加连接器和保险丝种类

## 1.6.1

### Fixes
- fix: 添加列表多选、排序功能
- fix: 添加产品预设备注显示
- fix: 添加Cluster总长度显示
- fix: 引入IdentifierRegistry以解决长度解析滞后问题

### Other
- chore: 说明更新

## 1.6.0

### Features
- feat: return confirmed workflow to update info
- feat: AI
- feat: add configurable AI service framework

### Fixes
- fix: 在row detail中添加offset显示
- fix: 将Single Line的绘制方向改为横向
- fix: 修复外部主线铝漏传首个支线坐标导致的长度值缺失
- fix:粘贴节点时默认展开

### Other
- refactor: framework
- refactor: optimize framework
- optimize: upload pdf window
- optimize: framework
- fix：生成single line；产品结构表性能改进
- optimize: flamework
- 修改CI目标分支为master

## 1.5.0

### Features
- feat: 添加LYNX产品生成逻辑，添加OCR服务

### Fixes
- fix: 生成LYNX产品时，将IBEX的whip定为0

### Other
- 更新说明
- 工作表导出进度修复
- 完善凑数据功能
- 提高溯源精度
- 添加压降错误信息溯源
- 添加属性复制粘贴功能
- 添加串数显示，添加Details列宽调整，修复长度表达式解析错误弹窗
- 添加凑数据的功能
- 添加了面板数量统计
- LYNX支线铝股数确定
- 完善了LYNX产品的生成逻辑
- 指定远程HEAD
- 指定UTF8编码
- 显示文件体积
- 更新runner目录
- 添加错误捕获和处理机制
- 修复更新器缺失的函数
- 修复命名逻辑
- 使gitlab根据当前commit自动创建tag并关联release
- 修复变量解析错误
- 修复转义错误
- 消除ci文件中的变量解析歧义
- 修复yml语法错误
- 修复路径不存在导致的命令报错问题
- 修复报错
- 移除多余测试
- 添加gitlab CI流水线
- 添加全屏幕截图支持
- 添加lynx产品的合并逻辑
- 添加OCR服务和截图选择功能
- 添加历史查询功能
- 对应关系表中添加分表以显示不同类型的产品
- 防止删除唯一的slot
- 添加lynx产品的生成逻辑
- 自动检查更新时只看当前版本所在通道
- 调整fuse部件的电流排序，自动选择供应商
- 修改homerun_extender的描述逻辑和接头生成逻辑
- 添加导出进度表
- 对应关系表导出

## 1.4.0

### Features
- feat: 添加自动更新机制 (#30)
- feat: 添加串数统计显示和操作历史树 (#28)
- feat: 添加homerun (#24)
- feat: 添加pyinstaller和自动发布exe
- feat: 添加版号系统

### Fixes
- fix: 数值精度更新&修复下载报错问题 (#46)
- fix: 修复IEBX无主线保险丝时Description生成错误的问题和末端支线无保险丝的问题 (#44)
- fix: 修复版本号故障 (#42)
- fix: 杂项修复 (#40)
- fix: 杂项更新 (#36)
- fix: 杂项修复 (#32)
- fix: 变更 (#22)
- fix: 修复编译文件名匹配问题 (#19)
- fix: 修复cual中已弃用的参数
- fix: 字段更名
- fix: 修复更新问题
- fix: 更好的版本显示
- fix: 修复相对导入问题
- fix: 修复导入问题
- fix: 在release-please之后编译exe
- fix: 更新数据文档
- fix: switch release-please to simple type
- fix: 添加release-please manifest

### Other
- 添加LYNX产品的生成
- Revert "Merge branch 'dev' into 'main'"
- 添加LYNX产品的生成
- Revert "Merge branch 'dev' into 'main'"
- 添加LYNX产品的生成
- chore(main): release 1.3.3 (#47)
- chore(main): release 1.3.2 (#45)
- chore(main): release 1.3.1 (#43)
- Revert "fix: 杂项更新 (#36)" (#38)
- Revert "fix: 杂项修复 (#32)" (#34)
- chore(main): release 1.2.0 (#31)
- chore(main): release 1.3.0 (#29)
- chore(main): release 1.2.0 (#26)
- Dev (#25)
- 变更 (#21)
- chore(main): release 1.1.8 (#20)
- chore(main): release 1.1.7
- chore(main): release 1.1.6
- chore(main): release 1.1.5
- chore(main): release 1.1.4
- chore(main): release 1.1.3
- chore(main): release 1.1.2
- chore(main): release 1.1.1
- chore: 修复未编译的问题
- chore(main): release 1.1.0
- 功能：添加了导入导出功能
- 修复：更新说明文档
- 功能：连接器参数联动
- 修复：将extender更名为gap extender
- 功能：添加Extender的生成逻辑
- 修复：修复由于长度检测导致的文件生成错误
- 功能：完善长度标识符逻辑
- 修复：更新启动脚本，避免重复安装uv
- 修复：更新总parts列表和产品单独parts列表的克隆逻辑，修复长度相互干扰的现象
- 修复：移除旧的生成文件
- 修复：调整extender添加逻辑
- 修复: 对不同的线缆规格使用不同的字母前缀
- 修复：各类格式修改以匹配出图程序
- 修复：调整IBEX 外部主线生成逻辑
- 修复：调整部件和半成品的名称生成逻辑
- 修复：修正pitch的拼写错误
- 修复：出图逻辑更新
- 修复：修正了overmold和T kit的逻辑
- 功能：添加了products info的部分填写逻辑
- 功能：添加extender类型
- 功能：添加了Extender类型
- 功能：添加出图程序表中其他页面的生成
- 修复：移动生成部分到单独包中
- 修复：完善去重功能
- 功能：添加LYNX_Preset功能
- 修复：修复长度解析问题
- 功能：初步实现产品生成功能
- 修复：移除无用参数，完善产品生成逻辑
- 修复：将项目中的product更名到product presets
- 修复：修改了一些字段名称
- 修复：完善遗漏的字段
- 功能：添加多语言支持
- 功能：添加了product preset，将product变为专门用于产品实例的类
- 功能：添加LYNX和IBEX的产品preset和详细参数
- 修复：重写alt+123快捷键逻辑
- 清理遗留的薄包装
- 前端架构切换到BFF模式
- 修复tab中的一些问题
- 初始提交

## [1.3.3](https://github.com/guilimao/Count_and_Design/compare/v1.3.2...v1.3.3) (2026-06-23)


### Bug Fixes

* 数值精度更新&修复下载报错问题 ([#46](https://github.com/guilimao/Count_and_Design/issues/46)) ([341ebc4](https://github.com/guilimao/Count_and_Design/commit/341ebc4a48fad479f406a71dfa5c4f0f44c98dac))

## [1.3.2](https://github.com/guilimao/Count_and_Design/compare/v1.3.1...v1.3.2) (2026-06-22)


### Bug Fixes

* Project表新增三个项目 ([8ff290b](https://github.com/guilimao/Count_and_Design/commit/8ff290b2c2a3cb84d2778a1132986ea7c1990e54))
* 修复IEBX无主线保险丝时Description生成错误的问题和末端支线无保险丝的问题 ([8ff290b](https://github.com/guilimao/Count_and_Design/commit/8ff290b2c2a3cb84d2778a1132986ea7c1990e54))
* 修复IEBX无主线保险丝时Description生成错误的问题和末端支线无保险丝的问题 ([#44](https://github.com/guilimao/Count_and_Design/issues/44)) ([8ff290b](https://github.com/guilimao/Count_and_Design/commit/8ff290b2c2a3cb84d2778a1132986ea7c1990e54))
* 修改树解析逻辑，防止长历史树爆栈 ([8ff290b](https://github.com/guilimao/Count_and_Design/commit/8ff290b2c2a3cb84d2778a1132986ea7c1990e54))
* 连接器自动选择厂家 ([8ff290b](https://github.com/guilimao/Count_and_Design/commit/8ff290b2c2a3cb84d2778a1132986ea7c1990e54))

## [1.3.1](https://github.com/guilimao/Count_and_Design/compare/v1.3.0...v1.3.1) (2026-06-18)


### Features

* 添加homerun逻辑 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加操作历史 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加统计串数显示 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加自动更新机制 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加自动更新机制 ([#30](https://github.com/guilimao/Count_and_Design/issues/30)) ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))


### Bug Fixes

* reset release-please target version ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 为操作历史树添加人性化解释 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 修复change中的引用错误 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 修复racking pitch无法设置为空的问题 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 修复导致版本故障的Change log ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 修复导致版本故障的Change log ([93983bc](https://github.com/guilimao/Count_and_Design/commit/93983bcaf384a9bea3ab0502b17d67766250079c))
* 修复版本号故障 ([#42](https://github.com/guilimao/Count_and_Design/issues/42)) ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 修复编译文件名匹配问题 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 修复长度标识符遇到保留单位时的报错 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 完善了穿管的计算逻辑 ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 完善了穿管的计算逻辑 ([93983bc](https://github.com/guilimao/Count_and_Design/commit/93983bcaf384a9bea3ab0502b17d67766250079c))
* 完善了穿管的计算逻辑 ([1a7a24e](https://github.com/guilimao/Count_and_Design/commit/1a7a24ee38cab6639b0dde962a165dd1b4ad0c9e))
* 未使用铜铝转接的IBEX产品使用Harness名称 ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 未使用铜铝转接的IBEX产品使用Harness名称 ([93983bc](https://github.com/guilimao/Count_and_Design/commit/93983bcaf384a9bea3ab0502b17d67766250079c))
* 未使用铜铝转接的IBEX产品使用Harness名称 ([1a7a24e](https://github.com/guilimao/Count_and_Design/commit/1a7a24ee38cab6639b0dde962a165dd1b4ad0c9e))
* 杂项修复 ([#32](https://github.com/guilimao/Count_and_Design/issues/32)) ([5f33ad4](https://github.com/guilimao/Count_and_Design/commit/5f33ad4b5700f747198bb1391ad1a6a716fcdb2c))
* 杂项修复 ([#40](https://github.com/guilimao/Count_and_Design/issues/40)) ([93983bc](https://github.com/guilimao/Count_and_Design/commit/93983bcaf384a9bea3ab0502b17d67766250079c))
* 杂项更新 ([#36](https://github.com/guilimao/Count_and_Design/issues/36)) ([1a7a24e](https://github.com/guilimao/Count_and_Design/commit/1a7a24ee38cab6639b0dde962a165dd1b4ad0c9e))
* 添加报错提示框 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加操作历史树的翻译 ([3dc9ceb](https://github.com/guilimao/Count_and_Design/commit/3dc9cebe419535e587a678c4619b4ca6fab9de34))
* 添加铜铝转接阈值 ([69c9b93](https://github.com/guilimao/Count_and_Design/commit/69c9b93ff2a4c2aa59ff608c874e79e247f6a02e))
* 添加铜铝转接阈值 ([93983bc](https://github.com/guilimao/Count_and_Design/commit/93983bcaf384a9bea3ab0502b17d67766250079c))
* 添加铜铝转接阈值 ([1a7a24e](https://github.com/guilimao/Count_and_Design/commit/1a7a24ee38cab6639b0dde962a165dd1b4ad0c9e))

## [1.3.0](https://github.com/guilimao/Count_and_Design/compare/v1.2.0...v1.3.0) (2026-06-18)


### Features

* 添加串数统计显示和操作历史树 ([#28](https://github.com/guilimao/Count_and_Design/issues/28)) ([2d119f8](https://github.com/guilimao/Count_and_Design/commit/2d119f8f6bb7ea06cdba68604230403515c53c39))

## [1.2.0](https://github.com/guilimao/Count_and_Design/compare/v1.1.8...v1.2.0) (2026-06-16)


### Features

* 添加homerun ([#24](https://github.com/guilimao/Count_and_Design/issues/24)) ([9447601](https://github.com/guilimao/Count_and_Design/commit/9447601fc03e22c8d5b7d22a36ce8fe22cc514b4))


### Bug Fixes

* 修复点选none时的Bug ([9447601](https://github.com/guilimao/Count_and_Design/commit/9447601fc03e22c8d5b7d22a36ce8fe22cc514b4))
* 修复点选none时的Bug ([e769eba](https://github.com/guilimao/Count_and_Design/commit/e769eba57c8be1f0534cfd5d6e074a546dff3730))
* 允许支线为零的占位符 ([9447601](https://github.com/guilimao/Count_and_Design/commit/9447601fc03e22c8d5b7d22a36ce8fe22cc514b4))
* 允许支线为零的占位符 ([e769eba](https://github.com/guilimao/Count_and_Design/commit/e769eba57c8be1f0534cfd5d6e074a546dff3730))
* 变更 ([#22](https://github.com/guilimao/Count_and_Design/issues/22)) ([e769eba](https://github.com/guilimao/Count_and_Design/commit/e769eba57c8be1f0534cfd5d6e074a546dff3730))
* 禁用下拉框的滚轮 ([9447601](https://github.com/guilimao/Count_and_Design/commit/9447601fc03e22c8d5b7d22a36ce8fe22cc514b4))
* 禁用下拉框的滚轮 ([e769eba](https://github.com/guilimao/Count_and_Design/commit/e769eba57c8be1f0534cfd5d6e074a546dff3730))

## [1.1.8](https://github.com/guilimao/Count_and_Design/compare/v1.1.7...v1.1.8) (2026-06-15)


### Bug Fixes

* 修复编译文件名匹配问题 ([#19](https://github.com/guilimao/Count_and_Design/issues/19)) ([51382e7](https://github.com/guilimao/Count_and_Design/commit/51382e7eaa425295c9516af72d8a1d3a5871afc8))

## [1.1.7](https://github.com/guilimao/Count_and_Design/compare/v1.1.6...v1.1.7) (2026-06-15)


### Bug Fixes

* 修复cual中已弃用的参数 ([8a58ff8](https://github.com/guilimao/Count_and_Design/commit/8a58ff8f7fba5b3a7bfb9ad9250d2af50988d619))
* 修复cual中已弃用的参数 ([b45b8f0](https://github.com/guilimao/Count_and_Design/commit/b45b8f0031126b2a666d87b58db113e077577ea5))

## [1.1.6](https://github.com/guilimao/Count_and_Design/compare/v1.1.5...v1.1.6) (2026-06-15)


### Bug Fixes

* 字段更名 ([743e707](https://github.com/guilimao/Count_and_Design/commit/743e707be0a1e965f21ef433f9e588b175d275f0))
* 字段更名 ([f5ad2f0](https://github.com/guilimao/Count_and_Design/commit/f5ad2f0d3b2ccafa97e726851b04470103a6eeec))

## [1.1.5](https://github.com/guilimao/Count_and_Design/compare/v1.1.4...v1.1.5) (2026-06-12)


### Bug Fixes

* 修复更新问题 ([229965e](https://github.com/guilimao/Count_and_Design/commit/229965e4709a4164fab3a7ff7cae570944574757))
* 修复更新问题 ([5294181](https://github.com/guilimao/Count_and_Design/commit/5294181729a4bfb600bea992a9f255cbbc677a2f))

## [1.1.4](https://github.com/guilimao/Count_and_Design/compare/v1.1.3...v1.1.4) (2026-06-12)


### Bug Fixes

* 修复相对导入问题 ([bdb7b41](https://github.com/guilimao/Count_and_Design/commit/bdb7b41616873b62389b2d5061e3120b4486c0c8))
* 更好的版本显示 ([d23bd70](https://github.com/guilimao/Count_and_Design/commit/d23bd70a5a6d0f12a5c292507e5c7fbeb7746798))

## [1.1.3](https://github.com/guilimao/Count_and_Design/compare/v1.1.2...v1.1.3) (2026-06-12)


### Bug Fixes

* 修复导入问题 ([2d6aa7c](https://github.com/guilimao/Count_and_Design/commit/2d6aa7c0eaf27c9e97f245477e206cce0a550fd9))
* 修复导入问题 ([4a9da0d](https://github.com/guilimao/Count_and_Design/commit/4a9da0d7f7d34bd0fbea60894e943e6331f4b466))

## [1.1.2](https://github.com/guilimao/Count_and_Design/compare/v1.1.1...v1.1.2) (2026-06-12)


### Bug Fixes

* 在release-please之后编译exe ([e11df3a](https://github.com/guilimao/Count_and_Design/commit/e11df3a65c557d0ca01f1152d2faf5c2a0ceb11a))
* 在release-please之后编译exe ([debf039](https://github.com/guilimao/Count_and_Design/commit/debf0394724a803f780b75eb30c8c2cd51c9182d))

## [1.1.1](https://github.com/guilimao/Count_and_Design/compare/v1.1.0...v1.1.1) (2026-06-12)


### Bug Fixes

* 更新数据文档 ([b2c93a6](https://github.com/guilimao/Count_and_Design/commit/b2c93a605022c319aae6de3a0e39cc85793ff05c))
* 更新数据文档 ([31af081](https://github.com/guilimao/Count_and_Design/commit/31af08171cb150daaf256d8c48b4bf8c8038ff94))

## [1.1.0](https://github.com/guilimao/Count_and_Design/compare/v1.0.0...v1.1.0) (2026-06-12)


### Features

* 添加pyinstaller和自动发布exe ([d3ab706](https://github.com/guilimao/Count_and_Design/commit/d3ab7065e657bf7384290c65dcc00a93d3d06005))
* 添加版号系统 ([0f31367](https://github.com/guilimao/Count_and_Design/commit/0f31367e9f9995969622f0a808b0b0a3409c26fd))
* 添加版号系统 ([b21fb6b](https://github.com/guilimao/Count_and_Design/commit/b21fb6bd4636667f2622b749c2f8b9a48e29e8dd))


### Bug Fixes

* 添加release-please manifest ([3a8ee2f](https://github.com/guilimao/Count_and_Design/commit/3a8ee2f1814d9ffa92c738b63a5bb02a9d1e26eb))

## Changelog

All notable changes to Count and Design will be documented in this file.

This file is automatically maintained by [Release Please](https://github.com/googleapis/release-please).  
Please use [Conventional Commits](https://www.conventionalcommits.org/) for all commits.
