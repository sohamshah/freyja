/**
 * Moved to `src/shared/artifactMeta.ts` so the main process can run the same
 * extraction while building the global artifact index — the index precomputes
 * title/excerpt per artifact, which is what keeps the cross-session browser
 * from issuing ~1 700 file reads to render one list.
 *
 * Re-exported here so existing renderer imports keep working.
 */
export { extractArtifactMeta, type ArtifactMeta } from '@shared/artifactMeta'
