import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import DesktopShell from './DesktopShell.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <DesktopShell />
  </StrictMode>,
)
