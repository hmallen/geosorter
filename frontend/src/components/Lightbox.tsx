import { posterUrl, previewUrl, videoUrl } from '../api'
import type { LibraryFeature } from '../types'

interface Props {
  files: LibraryFeature[]
  index: number
  onIndex: (index: number) => void
  onClose: () => void
}

export default function Lightbox({ files, index, onIndex, onClose }: Props) {
  const f = files[index]
  if (!f) return null
  const prev = () => onIndex((index - 1 + files.length) % files.length)
  const next = () => onIndex((index + 1) % files.length)

  return (
    <div className="lightbox" onClick={onClose}>
      <div className="lightbox-body" onClick={(e) => e.stopPropagation()}>
        {f.properties.media_type === 'video' ? (
          <video
            src={videoUrl(f.properties.path)}
            poster={posterUrl(f.properties.path)}
            controls
            autoPlay
          />
        ) : (
          <img src={previewUrl(f.properties.path)} alt={f.properties.filename} />
        )}
        <div className="lightbox-nav">
          <button onClick={prev} aria-label="Previous">‹</button>
          <span>{f.properties.filename}</span>
          <button onClick={next} aria-label="Next">›</button>
          <button onClick={onClose} aria-label="Close">×</button>
        </div>
      </div>
    </div>
  )
}
