import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { CircleX } from 'lucide-react'

export interface SummaryError {
  id: string
  text: string
}

/**
 * GOV.UK error-summary pattern: every failure is listed once at the top and
 * links to the control that fixes it (design-system.service.gov.uk).
 */
export function ErrorSummary({ errors }: { errors: SummaryError[] }) {
  if (errors.length === 0) return null
  return (
    <Alert variant="destructive" role="alert" aria-labelledby="error-summary-title">
      <CircleX aria-hidden />
      <AlertTitle id="error-summary-title">
        {errors.length === 1 ? 'There is a problem' : `There are ${errors.length} problems`}
      </AlertTitle>
      <AlertDescription>
        <ul className="list-disc space-y-1 pl-4">
          {errors.map((e) => (
            <li key={e.id}>
              <a href={`#${e.id}`} className="underline underline-offset-2 hover:no-underline">
                {e.text}
              </a>
            </li>
          ))}
        </ul>
      </AlertDescription>
    </Alert>
  )
}
