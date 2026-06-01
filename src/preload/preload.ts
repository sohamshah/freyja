import { contextBridge, ipcRenderer } from 'electron'
import {
  IPC,
  type BridgeCommand,
  type BridgeEvent,
  type AppInfo,
  type BridgeMode,
  type ArtifactReadResult,
  type GatewayStatus,
  type SlackVerifyResult,
  type SlackManifestResult,
  type SimpleResult,
  type SkillRollupResult,
  type SkillListResult,
  type SkillReadResult,
} from '../shared/events.js'

type EventListener = (event: BridgeEvent) => void

const listeners = new Set<EventListener>()

ipcRenderer.on(IPC.bridgeEvent, (_e, payload: BridgeEvent) => {
  for (const l of listeners) {
    try {
      l(payload)
    } catch (err) {
      console.error('[preload] listener error', err)
    }
  }
})

const api = {
  onEvent(listener: EventListener): () => void {
    listeners.add(listener)
    return () => listeners.delete(listener)
  },
  async sendCommand(cmd: BridgeCommand): Promise<{ ok: boolean; error?: string }> {
    return ipcRenderer.invoke(IPC.sendCommand, cmd)
  },
  async getMode(): Promise<BridgeMode> {
    return ipcRenderer.invoke(IPC.getMode)
  },
  async requestDemoBurst(): Promise<{ ok: boolean }> {
    return ipcRenderer.invoke(IPC.requestDemoBurst)
  },
  async restartBridge(): Promise<{ ok: boolean; error?: string }> {
    return ipcRenderer.invoke(IPC.restartBridge)
  },
  async openExternal(url: string): Promise<{ ok: boolean }> {
    return ipcRenderer.invoke(IPC.openExternal, url)
  },
  async getAppInfo(): Promise<AppInfo> {
    return ipcRenderer.invoke(IPC.appInfo)
  },
  async sessionList(): Promise<{ ok: boolean; sessions?: any[]; error?: string }> {
    return ipcRenderer.invoke(IPC.sessionList)
  },
  async sessionLoad(id: string): Promise<{ ok: boolean; session?: any; error?: string }> {
    return ipcRenderer.invoke(IPC.sessionLoad, id)
  },
  async sessionSave(payload: any): Promise<{ ok: boolean; bytes?: number; durationMs?: number; error?: string }> {
    return ipcRenderer.invoke(IPC.sessionSave, payload)
  },
  async sessionIndexSave(payload: any[]): Promise<{ ok: boolean; bytes?: number; durationMs?: number; error?: string }> {
    return ipcRenderer.invoke(IPC.sessionIndexSave, payload)
  },
  async sessionDelete(id: string): Promise<{ ok: boolean; error?: string }> {
    return ipcRenderer.invoke(IPC.sessionDelete, id)
  },
  async compactionMetrics(): Promise<{ ok: boolean; rows?: any[]; error?: string }> {
    return ipcRenderer.invoke(IPC.compactionMetrics)
  },
  async sessionExport(id: string): Promise<{
    ok: boolean
    cancelled?: boolean
    jsonPath?: string
    /** Three-view (raw / live / compactions) bundle sibling. */
    bundlePath?: string
    tracePath?: string
    error?: string
  }> {
    return ipcRenderer.invoke(IPC.sessionExport, id)
  },
  async settingsGet(): Promise<{ ok: boolean; settings?: any; error?: string }> {
    return ipcRenderer.invoke(IPC.settingsGet)
  },
  async settingsUpdate(
    patch: any,
  ): Promise<{ ok: boolean; settings?: any; error?: string }> {
    return ipcRenderer.invoke(IPC.settingsUpdate, patch)
  },
  async artifactRead(filePath: string): Promise<ArtifactReadResult> {
    return ipcRenderer.invoke(IPC.artifactRead, filePath)
  },
  async artifactWrite(
    filePath: string,
    content: string,
  ): Promise<{ ok: boolean; error?: string }> {
    return ipcRenderer.invoke(IPC.artifactWrite, filePath, content)
  },
  // ── Gateway / Slack setup ─────────────────────────────────
  async gatewayStatus(): Promise<GatewayStatus> {
    return ipcRenderer.invoke(IPC.gatewayStatus)
  },
  async gatewayInstall(): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.gatewayInstall)
  },
  async gatewayUninstall(): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.gatewayUninstall)
  },
  async gatewayStart(): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.gatewayStart)
  },
  async gatewayStop(): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.gatewayStop)
  },
  async slackManifest(): Promise<SlackManifestResult> {
    return ipcRenderer.invoke(IPC.slackManifest)
  },
  async slackCopyManifest(): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.slackCopyManifest)
  },
  async slackVerifyTokens(
    botToken: string,
    appToken: string,
  ): Promise<SlackVerifyResult> {
    return ipcRenderer.invoke(IPC.slackVerifyTokens, botToken, appToken)
  },
  async slackSaveTokens(
    botToken: string,
    appToken: string,
  ): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.slackSaveTokens, botToken, appToken)
  },
  async slackSetAllowlist(
    teamId: string,
    userIds: string[],
    enforce: boolean,
  ): Promise<SimpleResult> {
    return ipcRenderer.invoke(IPC.slackSetAllowlist, teamId, userIds, enforce)
  },
  async slackGetConfig(): Promise<{
    ok: boolean
    enforce?: boolean
    allowedByWorkspace?: Record<string, string[]>
    error?: string
  }> {
    return ipcRenderer.invoke(IPC.slackGetConfig)
  },
  async llmKeysProbe(): Promise<import('../shared/events').LlmKeysProbeResult> {
    return ipcRenderer.invoke(IPC.llmKeysProbe)
  },
  // ── Skill-learning surface ────────────────────────────────
  async skillRollup(skillName: string): Promise<SkillRollupResult> {
    return ipcRenderer.invoke(IPC.skillRollup, skillName)
  },
  async skillListCandidates(): Promise<SkillListResult> {
    return ipcRenderer.invoke(IPC.skillListCandidates)
  },
  async skillListRejected(limit?: number): Promise<SkillListResult> {
    return ipcRenderer.invoke(IPC.skillListRejected, limit)
  },
  async skillReadFile(skillName: string): Promise<SkillReadResult> {
    return ipcRenderer.invoke(IPC.skillReadFile, skillName)
  },
  async skillSave(skillName: string, body: string): Promise<SimpleResult & { path?: string }> {
    return ipcRenderer.invoke(IPC.skillSave, skillName, body)
  },
  async skillOpen(skillName: string): Promise<SimpleResult & { path?: string }> {
    return ipcRenderer.invoke(IPC.skillOpen, skillName)
  },
} as const

export type HarnessApi = typeof api

contextBridge.exposeInMainWorld('harness', api)
