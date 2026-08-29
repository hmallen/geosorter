import type { ReactNode, SVGProps } from 'react'

// Hand-drawn 16px stroke icons for the toolbar. Authored inline (no icon
// dependency) so the app stays fully offline-bundled, like the self-hosted
// fonts. One shared stroke weight + round caps/joins keeps every control in
// the same drawing voice; color comes from currentColor so CSS owns the tint.
type IconProps = SVGProps<SVGSVGElement>

function Icon({ children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  )
}

export function InboxIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M2 8.75 3.42 4.4a1.3 1.3 0 0 1 1.24-.9h6.68a1.3 1.3 0 0 1 1.24.9L14 8.75v2.45a1.3 1.3 0 0 1-1.3 1.3H3.3a1.3 1.3 0 0 1-1.3-1.3Z" />
      <path d="M2 8.75h3.1l1.15 1.9h3.5l1.15-1.9H14" />
    </Icon>
  )
}

export function UndoIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M5.9 9.4 2.7 6.2 5.9 3" />
      <path d="M2.7 6.2h6.6a3.9 3.9 0 0 1 0 7.8H7.2" />
    </Icon>
  )
}

export function RescanIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14 8a6 6 0 1 1-1.76-4.24L14 5.5" />
      <path d="M14 2.5v3h-3" />
    </Icon>
  )
}

export function PinIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13 6.6c0 3.2-3.6 6.2-4.6 7a.63.63 0 0 1-.8 0c-1-.8-4.6-3.8-4.6-7a5 5 0 0 1 10 0Z" />
      <circle cx="8" cy="6.6" r="1.8" />
    </Icon>
  )
}

export function RouteIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="3.8" cy="12.2" r="1.7" />
      <circle cx="12.2" cy="3.8" r="1.7" />
      <path d="M5.5 12.2h4.2a2.35 2.35 0 0 0 0-4.7H6.3a2.35 2.35 0 0 1 0-4.7h4.2" />
    </Icon>
  )
}

export function TimelineIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M2.2 13.2h11.6" />
      <path d="M3.6 13.2V9.8" />
      <path d="M8 13.2V4.8" />
      <path d="M12.4 13.2V7.4" />
    </Icon>
  )
}

export function HeartIcon({ filled, ...rest }: IconProps & { filled?: boolean }) {
  return (
    <Icon {...rest} fill={filled ? 'currentColor' : 'none'}>
      <path d="M12.67 9.33c1-.97 2-2.13 2-3.66A3.67 3.67 0 0 0 11 2c-1.17 0-2 .33-3 1.33C7 2.33 6.17 2 5 2a3.67 3.67 0 0 0-3.67 3.67c0 1.53 1 2.69 2 3.66L8 14Z" />
    </Icon>
  )
}

export function PinOffIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13 6.6c0 3.2-3.6 6.2-4.6 7a.63.63 0 0 1-.8 0c-1-.8-4.6-3.8-4.6-7a5 5 0 0 1 10 0Z" />
      <path d="m2.5 2 11 11.5" />
    </Icon>
  )
}

export function CopyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="5.6" y="5.6" width="8.4" height="8.4" rx="1.4" />
      <path d="M3.2 10.4H3a1.4 1.4 0 0 1-1.4-1.4V3A1.4 1.4 0 0 1 3 1.6h6a1.4 1.4 0 0 1 1.4 1.4v.2" />
    </Icon>
  )
}

export function WrenchIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M13.6 2.5a3.5 3.5 0 0 0-4.7 4.4L2.5 13.2a1.55 1.55 0 0 0 2.2 2.2L11 9a3.5 3.5 0 0 0 4.4-4.7L13 6.7l-2.2-.7-.7-2.2Z" />
    </Icon>
  )
}

export function PanoramaIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M1.8 4.2C3.9 3.46 6 3.1 8 3.1s4.1.36 6.2 1.1v7.6c-2.1-.74-4.2-1.1-6.2-1.1s-4.1.36-6.2 1.1Z" />
    </Icon>
  )
}
