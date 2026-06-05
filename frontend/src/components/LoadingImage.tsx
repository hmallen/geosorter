import { useEffect, useRef, useState } from 'react'
import { MAX_IMG_RETRIES, retryDelayMs } from '../loadingImage'

interface Props {
  src: string
  alt: string
  className?: string // applied to the wrapper, so callers can scope context CSS
}

// An <img> that shows a spinner while loading and, on error, retries with backoff
// before giving up — so a freshly-organized derived asset still being generated
// server-side reads as "loading", not a permanently-broken link. Callers pass
// `key={src}` so a source change remounts this fresh (resetting the retry state);
// the existing `<container> img` CSS selectors keep applying (the img is still a
// descendant).
export default function LoadingImage({ src, alt, className }: Props) {
  const [status, setStatus] = useState<'loading' | 'loaded' | 'failed'>('loading')
  const [bust, setBust] = useState(0) // cache-buster bumped per retry to force a reload
  const attempt = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)

  // Clear any pending retry timer on unmount (a src change remounts via key={src}).
  useEffect(() => () => clearTimeout(timer.current), [])

  const onLoad = () => {
    clearTimeout(timer.current) // a success cancels any queued retry (no late flip-back)
    setStatus('loaded')
  }

  const onError = () => {
    clearTimeout(timer.current)
    if (attempt.current >= MAX_IMG_RETRIES) {
      setStatus('failed')
      return
    }
    attempt.current += 1
    timer.current = setTimeout(() => {
      setStatus('loading')
      setBust((b) => b + 1)
    }, retryDelayMs(attempt.current))
  }

  const url = bust > 0 ? `${src}${src.includes('?') ? '&' : '?'}_r=${bust}` : src

  return (
    <span className={`img-wrap ${className ?? ''}`}>
      {status !== 'failed' && (
        <img
          src={url}
          alt={alt}
          loading="lazy"
          onLoad={onLoad}
          onError={onError}
          style={{ visibility: status === 'loaded' ? 'visible' : 'hidden' }}
        />
      )}
      {status === 'loading' && <span className="img-spinner" aria-label="loading" />}
      {status === 'failed' && (
        <span className="img-failed" title="Couldn't load image">
          ⚠
        </span>
      )}
    </span>
  )
}
