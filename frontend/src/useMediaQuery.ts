import { useEffect, useState } from 'react'

// Subscribe to a CSS media query and re-render on change. Thin DOM glue (the testable
// snap math lives in the pure sheet.ts). SSR-safe guard so a non-browser env returns false.
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && 'matchMedia' in window
      ? window.matchMedia(query).matches
      : false,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || !('matchMedia' in window)) return
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange() // sync immediately in case the query changed between render and effect
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}

// Phone + tablet share the bottom-sheet layout; desktop (>1024px) keeps the right rail.
// The 1024px ceiling is the desktop breakpoint from the task's 3-tier design.
export function useIsMobile(): boolean {
  return useMediaQuery('(max-width: 1024px)')
}
