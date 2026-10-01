import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ThemeProvider } from 'next-themes'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { StoreProvider } from '@/prototype/store'

// Prototype-only: ?theme=light|dark lets a reviewer or the capture script pick
// a theme without touching the toggle. The real app follows the OS setting.
const paramTheme = new URLSearchParams(window.location.search).get('theme')
const initialTheme = () => (paramTheme === 'light' || paramTheme === 'dark' ? paramTheme : 'dark')

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme={initialTheme()} enableSystem disableTransitionOnChange>
      <TooltipProvider>
        <StoreProvider>
          <App />
          <Toaster position="bottom-right" />
        </StoreProvider>
      </TooltipProvider>
    </ThemeProvider>
  </StrictMode>,
)
