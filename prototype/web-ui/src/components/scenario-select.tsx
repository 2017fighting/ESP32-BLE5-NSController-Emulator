import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SCENARIOS, type ScenarioId } from '@/prototype/model'
import { useStore } from '@/prototype/store'

const GROUPS = ['Happy path', 'In progress', 'Failure & recovery', 'Amiibo lock'] as const

/** Prototype-only review aid: jumps the simulated device between states. */
export function ScenarioSelect() {
  const { state, setScenario } = useStore()
  return (
    <Select value={state.scenario} onValueChange={(v) => setScenario(v as ScenarioId)}>
      <SelectTrigger size="sm" className="w-[16rem] md:w-[20rem]" aria-label="Prototype scenario">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {GROUPS.map((g) => (
          <SelectGroup key={g}>
            <SelectLabel>{g}</SelectLabel>
            {SCENARIOS.filter((s) => s.group === g).map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.label}
              </SelectItem>
            ))}
          </SelectGroup>
        ))}
      </SelectContent>
    </Select>
  )
}
