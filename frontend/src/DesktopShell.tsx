import { useCallback, useEffect, useRef, useState } from 'react'
import App from './App'
import { AuthProvider } from './useAuth'
import { saveToken } from './auth'
import { connectDesktop, desktopRequest, DesktopActionError, progressText, type DesktopStatus } from './desktopApi'
import './DesktopShell.css'

export default function DesktopShell() {
  const [status, setStatus] = useState<DesktopStatus | null>()
  const [error, setError] = useState('')
  const [errorDetails, setErrorDetails] = useState<string>()
  const [settings, setSettings] = useState(false)
  const [step, setStep] = useState(0)
  const [inbox, setInbox] = useState('')
  const [library, setLibrary] = useState('')
  const [existing, setExisting] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [quitBusy, setQuitBusy] = useState(false)
  const [review, setReview] = useState(false)
  const [readyScreen, setReadyScreen] = useState(false)
  const [closed, setClosed] = useState(false)
  const title = useRef<HTMLHeadingElement>(null)

  const connect = useCallback(() => {
    connectDesktop().then(({ status: value, settings: open }) => {
      setError('')
      setStatus(value)
      setSettings(open)
      if (value) {
        setInbox(value.inbox_path)
        setLibrary(value.library_root)
        setStep(value.configured ? 2 : 0)
      }
    }).catch((reason) => setError(String(reason.message || reason)))
  }, [])
  useEffect(connect, [connect])
  const desktop = status != null
  useEffect(() => {
    if (!desktop || closed) return
    let alive = true
    let pending = false
    const timer = setInterval(async () => {
      if (pending) return
      pending = true
      try {
        const next = await desktopRequest<DesktopStatus>('status')
        if (alive) setStatus(next)
      } catch (reason) {
        if (alive) {
          if (status?.state === 'closing') setClosed(true)
          else setError(reason instanceof Error ? reason.message : 'Connection lost. Reopen GeoSorter to reconnect.')
        }
      } finally { pending = false }
    }, 1500)
    return () => { alive = false; clearInterval(timer) }
  }, [desktop, closed, status?.state])
  useEffect(() => { title.current?.focus() }, [step, settings, status?.state])

  async function action(task: () => Promise<void>) {
    setBusy(true)
    setError('')
    setErrorDetails(undefined)
    try { await task() } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Something went wrong. Please retry.')
      setErrorDetails(reason instanceof DesktopActionError ? reason.technicalDetails : undefined)
    } finally { setBusy(false) }
  }
  async function refresh() { setStatus(await desktopRequest<DesktopStatus>('retry', {})) }
  async function pick(kind: 'folder' | 'config', update: (value: string) => void) {
    const result = await desktopRequest<{ path: string | null }>('choose', { kind })
    if (result.path) update(result.path)
  }
  async function configure() {
    const body = { inbox_path: inbox, library_root: library }
    await desktopRequest('validate', body)
    setStatus(await desktopRequest<DesktopStatus>('configure', body))
    setStep(2)
  }
  async function start(kind: 'cities' | 'features' | 'untrunc') {
    await desktopRequest('jobs', { kind })
    if (kind === 'cities') setReadyScreen(true)
    setStatus(await desktopRequest<DesktopStatus>('status'))
  }
  async function quit(wait = false) {
    const result = await desktopRequest<{ busy: boolean; closing?: boolean }>('quit', { wait })
    setQuitBusy(result.busy)
    if (result.closing) setStatus((current) => current ? { ...current, state: 'closing' } : current)
  }
  async function diagnostics() {
    const data = await desktopRequest('diagnostics')
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url; link.download = 'geosorter-diagnostics.json'; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  if (status === null) return <AuthProvider><App /></AuthProvider>
  if (status?.state === 'ready' && !settings && !readyScreen && !quitBusy) {
    return <AuthProvider><App onDesktopSettings={() => setSettings(true)} reviewInbox={review} /></AuthProvider>
  }
  const running = status?.active_jobs.length !== 0 && desktop
  const disabled = busy || running || status?.state === 'closing'
  const recovery = status?.state === 'recovery'
  const ready = status?.state === 'ready'
  const job = status?.job
  const heading = closed ? 'GeoSorter is closed' : status?.state === 'closing' ? 'Finishing before closing'
    : quitBusy ? 'GeoSorter is still working' : settings ? 'Settings & Help' : recovery ? 'Let’s get you connected'
    : ready ? 'Your library is ready' : step === 0 ? 'A place for every capture' : step === 1 ? 'Choose your folders' : 'Prepare place data'

  return <main className="desktop-page">
    <header className="desktop-brand"><img src="/favicon.svg" alt="" width="28" height="28" />geosorter
      {status && <span>Preview · {status.version}</span>}</header>
    <div className="desktop-content">
      {!settings && !recovery && !closed && !quitBusy && status?.state !== 'closing' && <ol className="desktop-steps" aria-label="Setup progress">
        {['Welcome', 'Folders', 'Place data', 'Ready'].map((name, i) => <li key={name} aria-current={(ready ? 3 : step) === i ? 'step' : undefined}><span>{i + 1}</span>{name}</li>)}
      </ol>}
      <h1 ref={title} tabIndex={-1}>{heading}</h1>
      <div aria-live="polite">
        {error && <p className="desktop-error" role="alert">{error}</p>}
        {status?.error && <p className="desktop-error" role="alert">{status.error}</p>}
        {(errorDetails || status?.error_details) && <details><summary>Technical details</summary><p className="desktop-location">{errorDetails || status?.error_details}</p></details>}
      </div>
      {status === undefined && <><p>{error ? 'Launch GeoSorter from its shortcut, or retry the connection.' : 'Connecting to GeoSorter…'}</p><button onClick={connect}>Retry connection</button></>}
      {closed && <p>You can close this tab. Open GeoSorter from its shortcut whenever you need it.</p>}
      {status?.state === 'closing' && !closed && <><p>Current work will finish safely. You can leave this tab open to see when GeoSorter closes.</p><ul>{status.active_jobs.map((item) => <li key={item.job_id}>{item.kind}: {item.state}</li>)}</ul></>}
      {quitBusy && <><p>Your media is still being processed. GeoSorter can close after the current work finishes.</p><div className="desktop-actions"><button onClick={() => setQuitBusy(false)}>Keep running</button><button className="primary" disabled={busy} onClick={() => action(() => quit(true))}>Quit when finished</button></div></>}
      {status && !closed && status.state !== 'closing' && !quitBusy && <>
        {status.auth_required && <details className="desktop-section"><summary>Administrator login</summary>
          <form onSubmit={(event) => { event.preventDefault(); action(async () => { const result = await desktopRequest<{ token: string }>('login', { password }); saveToken(result.token); setPassword(''); await refresh() }) }}>
            <label htmlFor="desktop-password">Administrator password</label><input id="desktop-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
            <button disabled={busy}>Log in</button>
          </form></details>}
        {recovery && <p>Check the items below, then retry. Your configuration and catalog have been kept.</p>}
        {!settings && !recovery && !ready && step === 0 && <>
          <p className="desktop-lead">Organize your drone photos and videos by location and date, then explore them on a map.</p>
          <p>Choose where incoming media lives and where your organized library should go. We’ll prepare the place names for you.</p>
          <button className="primary" onClick={() => setStep(1)}>Set up a new library</button>
        </>}
        {((step === 1 && !ready && !recovery) || settings) && <form className="desktop-section" onSubmit={(event) => { event.preventDefault(); action(configure) }}>
          <h2>Media folders</h2>
          <label htmlFor="desktop-inbox">Incoming media</label><p className="desktop-hint" id="inbox-help">The folder where you put new card dumps or recordings.</p>
          <div className="desktop-path"><input id="desktop-inbox" aria-describedby="inbox-help" value={inbox} required onChange={(event) => setInbox(event.target.value)} disabled={disabled} /><button type="button" disabled={disabled} onClick={() => action(() => pick('folder', setInbox))}>Browse…</button></div>
          <label htmlFor="desktop-library">Organized library</label><p className="desktop-hint" id="library-help">{status.configured ? 'Library relocation is not available in this preview.' : 'Choose an empty folder, separate from incoming media.'}</p>
          <div className="desktop-path"><input id="desktop-library" aria-describedby="library-help" value={library} required disabled={disabled || status.configured} onChange={(event) => setLibrary(event.target.value)} />{!status.configured && <button type="button" disabled={disabled} onClick={() => action(() => pick('folder', setLibrary))}>Browse…</button>}</div>
          {!status.configured && <button type="button" disabled={disabled || !library} onClick={() => action(async () => { await desktopRequest('create-folder', { path: library }) })}>Create folder at this path</button>}
          <p>Organizing moves media from your incoming folder into the library. Setup only saves these locations; you’ll review the inbox before starting an import.</p>
          <div className="desktop-actions">{!status.configured && <button type="button" onClick={() => setStep(0)}>Back</button>}<button className="primary" disabled={disabled}>{settings ? 'Save incoming folder' : 'Save folders & continue'}</button></div>
        </form>}
        {!status.configured && <details className="desktop-section"><summary>Use existing GeoSorter configuration</summary>
          <p>Choose the original configuration and keep its catalog, annotations, and organized media.</p>
          <label htmlFor="existing-config">Configuration file</label><div className="desktop-path"><input id="existing-config" value={existing} onChange={(event) => setExisting(event.target.value)} /><button disabled={disabled} onClick={() => action(() => pick('config', setExisting))}>Browse…</button></div>
          <label htmlFor="existing-password">Administrator password, if configured</label><input id="existing-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
          <button disabled={disabled || !existing} onClick={() => action(async () => { const next = await desktopRequest<DesktopStatus>('adopt', { path: existing, password }); setStatus(next); setInbox(next.inbox_path); setLibrary(next.library_root); setPassword(''); setStep(2) })}>Use this configuration</button>
        </details>}
        {status.configured && !status.geonames_ready && !recovery && <section className="desktop-section">
          <p>Download city and region names so GeoSorter can organize captures by place. This requires internet access once; the place index stays on this computer.</p>
          <p>Parks, peaks, and lakes are available later in Extras.</p>
          <button className="primary" disabled={disabled} onClick={() => action(() => start('cities'))}>{job?.state === 'error' || job?.state === 'interrupted' ? 'Retry place data setup' : 'Download & prepare place data'}</button>
        </section>}
        {job && (job.state !== 'done' || settings) && <section className="desktop-section" aria-live="polite">
          <h2>{job.kind === 'untrunc' ? 'Video repair setup' : 'Place data setup'}</h2><p>{progressText(job)}</p>
          {(job.state === 'pending' || job.state === 'running') && <progress aria-label="Setup progress" max={job.total || undefined} value={job.total ? job.done : undefined} />}
          {job.current && <p className="desktop-hint">{job.current}</p>}
          {job.details && <details><summary>Technical details</summary><p className="desktop-location">{job.details}</p></details>}
          {(job.state === 'error' || job.state === 'interrupted') && <button disabled={disabled} onClick={() => action(() => start(job.kind))}>Retry</button>}
        </section>}
        {(settings || recovery) && <section className="desktop-section"><h2>Connection & tools</h2>
          <ul className="desktop-checks">{status.checks.map((item) => <li key={item.name}><strong>{item.name}</strong><span className={item.ok ? '' : 'desktop-error'}>{item.ok ? 'Ready' : item.message}</span></li>)}</ul>
          <p className="desktop-hint">The map background needs an internet connection. Your media and catalog stay on your drives.</p>
          <button disabled={disabled} onClick={() => action(refresh)}>Check again / Retry</button>
        </section>}
        {settings && status.configured && <section className="desktop-section"><h2>Extras</h2>
          {status.extras.hugin_message && <p className="desktop-error">{status.extras.hugin_message}</p>}
          <div className="desktop-extra"><div><h3>Parks, peaks & lakes</h3><p role="status">{status.extras.detailed_places ? 'Detailed places are installed and ready to use.' : 'A larger place-name download for wilderness captures.'}</p></div><button disabled={disabled || status.extras.detailed_places} onClick={() => action(() => start('features'))}>{status.extras.detailed_places ? 'Installed' : job?.kind === 'features' && running ? 'Preparing…' : 'Download detailed places'}</button></div>
          <div className="desktop-extra"><div><h3>Panorama stitching</h3><p>{status.extras.hugin ? 'Hugin is ready.' : 'Install Hugin, then select its bin folder.'}</p><a href="https://hugin.sourceforge.io/download/" target="_blank" rel="noreferrer">Download Hugin</a></div><button disabled={disabled} onClick={() => action(async () => { const result = await desktopRequest<{ path: string | null }>('choose', { kind: 'folder' }); if (result.path) setStatus(await desktopRequest<DesktopStatus>('hugin', { path: result.path })) })}>Locate installation</button></div>
          <div className="desktop-extra"><div><h3>Video repair</h3><p>{status.extras.untrunc ? 'Video repair is ready.' : 'Add untrunc to help recover interrupted recordings.'}</p></div><button disabled={disabled || status.extras.untrunc} onClick={() => action(() => start('untrunc'))}>{status.extras.untrunc ? 'Installed' : 'Install video repair'}</button></div>
        </section>}
        {ready && !settings && <><p className="desktop-lead">Your folders and place data are prepared. Review incoming captures whenever you’re ready.</p><p>Closing the browser leaves GeoSorter running. Use Settings & Help or the GeoSorter tray icon to quit.</p><button className="primary" onClick={() => { setReadyScreen(false); setReview(true); setSettings(false) }}>Review inbox</button><button onClick={() => { setReadyScreen(false); setSettings(false) }}>Open library</button></>}
        {(settings || recovery) && <section className="desktop-section"><h2>Help & updates</h2><p>Install a newer preview over this one to keep your settings and library.</p><div className="desktop-actions"><a href="https://github.com/hmallen/geosorter/releases" target="_blank" rel="noreferrer">Check for updates</a><button disabled={busy} onClick={() => action(diagnostics)}>Export diagnostics</button></div><details><summary>Configuration location</summary><p className="desktop-hint desktop-location">{status.config_path}</p></details></section>}
        <footer className="desktop-actions desktop-footer">{settings && ready && <button className="primary" onClick={() => setSettings(false)}>Back to library</button>}<button disabled={busy} onClick={() => action(() => quit())}>Quit GeoSorter</button></footer>
      </>}
    </div>
  </main>
}
