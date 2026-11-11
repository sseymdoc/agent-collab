// 当前连接的身份与 controller 能力（GET /client）。界面按它隐藏做不了的操作；真正的权限检查在 controller。
import type { J } from './api'

let current: { identity: J; controller: J } | null = null

export function setSession(v: { identity: J; controller: J } | null) { current = v }
export const session = () => current
export const can = (scope: string) => !!current?.identity?.scopes?.includes(scope)
