# astrbot-plugin-passonspeaker

私聊转发辅助插件。仅 AstrBot 管理员可在私聊中使用。

## Commands

- `/passon bind <umo1 umo2...>`
  - 绑定当前私聊会话的默认转发目标。
- `/passon status`
  - 查看当前默认目标。
- `/passon unbind`
  - 解除当前默认目标。
- `/passon send <message> --umo <umo1 umo2...>`
  - 将文本转发到指定目标。
- `/passon send <message>`
  - 当当前私聊已绑定默认目标时，转发文本到默认目标。
- `/passon send --umo <umo1 umo2...>` 并引用一条消息
  - 当命令正文为空且这条命令引用了一条消息时，转发被引用的那条消息。

## Notes

- `--sid` 仍然兼容，可与 `--umo` 混用。
- 默认绑定按管理员身份隔离，支持多个管理员同时绑定同一个 umo。
- 绑定关系会持久化到插件目录，插件重载或重启后会自动恢复。
- 为避免与其他私聊插件冲突，插件不再拦截普通私聊消息，所有转发都需要显式使用 `/passon` 命令。
