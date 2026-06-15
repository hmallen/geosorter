import { useState } from 'react'
import { runAssignLocation, type AssignState } from './assignLocationJob'

// React wrapper around the pure runAssignLocation driver, for the No-GPS panel.
// Two ways in: a place-name search hands an explicit coordinate (assignToCoord), or
// "Set on map" enters placement mode (beginAssign holds the selected ids) and the
// next map click resolves it (pickLocation, confirm-gated). Both fan one coordinate
// out to every selected no-GPS capture and fire onDone so the caller can reload.
export function useAssignLocation(onDone?: () => void) {
  const [targetIds, setTargetIds] = useState<number[] | null>(null)
  const [assign, setAssign] = useState<AssignState | null>(null)
  const [assigning, setAssigning] = useState(false)

  const beginAssign = (ids: number[]): void => setTargetIds(ids.length ? ids : null)
  const cancelAssign = (): void => setTargetIds(null)

  async function run(ids: number[], lat: number, lon: number): Promise<void> {
    if (assigning || ids.length === 0) return
    setTargetIds(null)
    setAssigning(true)
    try {
      const final = await runAssignLocation(fetch, ids, lat, lon, { onProgress: setAssign })
      setAssign(final)
      onDone?.()
    } catch (e) {
      setAssign({
        state: 'error', assigned: 0, skipped: 0, place_string: null,
        processed: 0, current: null, error: String(e), failures: [],
      })
    } finally {
      setAssigning(false)
    }
  }

  // Map-click resolution of an active placement (confirm-gated, like re-tag).
  async function pickLocation(lng: number, lat: number): Promise<void> {
    if (targetIds === null) return
    const ids = targetIds
    if (!window.confirm(`Assign this location to ${ids.length} no-GPS capture(s)?`)) return
    await run(ids, lat, lng)
  }

  // Place-name search resolution: the panel already confirmed with the place label.
  const assignToCoord = (ids: number[], lat: number, lon: number): Promise<void> =>
    run(ids, lat, lon)

  return {
    assign,
    assigning,
    placing: targetIds !== null,
    count: targetIds?.length ?? 0,
    beginAssign,
    cancelAssign,
    pickLocation,
    assignToCoord,
  }
}
