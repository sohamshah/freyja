// The call-resolving helpers live in shared code: the main process uses them
// to build exports. Components import them from here.
export { CALL_TOOL, resolveToolCall, type ResolvedToolCall } from '@shared/toolCall'
