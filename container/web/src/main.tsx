import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ThemeProvider } from 'next-themes'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { StoreProvider } from '@/app/store'

// The shipped app follows the OS theme (§8.8); there is no theme toggle and no
// `?theme` deep-link — those were prototype review affordances.
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <TooltipProvider>
        <StoreProvider>
          <App />
          <Toaster position="bottom-right" />
        </StoreProvider>
      </TooltipProvider>
    </ThemeProvider>
  </StrictMode>,
)
