import { useMemo, useRef, useState } from 'react'
import { runAssignLocation, type AssignState } from './assignLocationJob'
import { useAuthContext } from './useAuth'

interface TrackedAssign {
  fileIds: number[]
  state: AssignState
}

function pendingState(total: number): AssignState {
  return {
    state: 'pending', assigned: 0, skipped: 0, place_string: null,
    total, processed: 0, current: null, error: null, failures: [],
  }
}

// React wrapper around the pure runAssignLocation driver, for the No-GPS panel.
// Two ways in: a place-name search hands an explicit coordinate (assignToCoord), or
// "Set on map" enters placement mode (beginAssign holds the selected ids) and the
// next map click resolves it (pickLocation, confirm-gated). Both fan one coordinate
// out to every selected no-GPS capture. Assignments are tracked independently so a
// second location can be submitted while the server's single destructive worker is
// still processing the first; the server serializes those jobs safely.
export function useAssignLocation(onDone?: () => void) {
  const { authFetch } = useAuthContext()
  const [targetIds, setTargetIds] = useState<number[] | null>(null)
  const [jobs, setJobs] = useState<Record<number, TrackedAssign>>({})
  const nextKey = useRef(1)
  // Synchronous reservation prevents a rapid second click from queueing the same file
  // before React has rendered the first job's pending state.
  const reservedIds = useRef<Set<number>>(new Set())

  const activeJobs = useMemo(
    () => Object.values(jobs).filter(({ state }) =>
      state.state === 'pending' || state.state === 'running'),
    [jobs],
  )
  const assigning = activeJobs.length > 0
  const assign =
    activeJobs.find(({ state }) => state.state === 'running')?.state ??
    activeJobs[0]?.state ?? null
  const queuedCount = Math.max(0, activeJobs.length - 1)
  const queuedFileIds = useMemo(
    () => new Set(activeJobs.flatMap(({ fileIds }) => fileIds)),
    [activeJobs],
  )

  const beginAssign = (ids: number[]): void => setTargetIds(ids.length ? ids : null)
  const cancelAssign = (): void => setTargetIds(null)

  async function run(ids: number[], lat: number, lon: number): Promise<void> {
    const availableIds = [...new Set(ids)].filter((id) => !reservedIds.current.has(id))
    setTargetIds(null)
    if (availableIds.length === 0) return

    const key = nextKey.current++
    availableIds.forEach((id) => reservedIds.current.add(id))
    setJobs((current) => ({
      ...current,
      [key]: { fileIds: availableIds, state: pendingState(availableIds.length) },
    }))

    const update = (state: AssignState) =>
      setJobs((current) => ({
        ...current,
        [key]: { fileIds: availableIds, state },
      }))

    try {
      const final = await runAssignLocation(authFetch, availableIds, lat, lon, {
        onProgress: update,
      })
      update(final)
      onDone?.()
    } catch (e) {
      update({
        state: 'error', assigned: 0, skipped: 0, place_string: null,
        total: availableIds.length, processed: 0, current: null,
        error: String(e), failures: [],
      })
    } finally {
      availableIds.forEach((id) => reservedIds.current.delete(id))
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
    queuedCount,
    queuedFileIds,
    placing: targetIds !== null,
    count: targetIds?.length ?? 0,
    beginAssign,
    cancelAssign,
    pickLocation,
    assignToCoord,
  }
}
