import {
  atom, Badge, Box, Button, Callout, Code, CodeView, EmptyState, env, Input, KeyValue,
  List, ListItem, plugin, read, Row, Select, set, Spinner, Stack, Tab, Tabs, Text
} from 'orca'
import type { LocalizedText, MemoryStatus, RecordView, SearchItem } from './protocol'
import { preparePrompt, workflows } from './workflows'

const query = atom('')
const results = atom<SearchItem[] | null>(null)
const record = atom<RecordView | null>(null)
const status = atom<MemoryStatus | null>(null)
const pending = atom(false)
const requestError = atom('')
const action = atom('remember')
const argument = atom('')
const prompt = atom('')
const workflowError = atom('')

async function run(operation: () => Promise<void>) {
  if (read(pending)) return
  set(pending, true)
  set(requestError, '')
  try {
    await operation()
  } catch (error) {
    set(requestError, error instanceof Error ? error.message : String(error))
  } finally {
    set(pending, false)
  }
}

function search(value = read(query)) {
  if (!value.trim()) return
  return run(async () => {
    set(results, null)
    set(record, null)
    set(results, await plugin.request('amemo.search', { query: value }))
  })
}

function openRecord(item: SearchItem) {
  return run(async () => {
    set(record, null)
    set(record, await plugin.request('amemo.record', { ref: item.ref, revision: item.revision }))
  })
}

function refreshStatus() {
  return run(async () => {
    set(status, null)
    set(status, await plugin.request('amemo.status'))
  })
}

function localized(value: LocalizedText | null | undefined) {
  if (!value || typeof value === 'string') return value || ''
  const language = env.locale.split('-')[0]
  return value[env.locale] || value[language] || Object.values(value).find(Boolean) || ''
}

function Record({ value }: { value: RecordView }) {
  return (
    <Stack gap="sm">
      <Button variant="ghost" onPress={() => set(record, null)}>Back to results</Button>
      <Text weight="semibold">{localized(value.payload.label) || value.ref}</Text>
      <Text muted><Code>{value.ref}</Code> · revision {value.revision}</Text>
      <Text>{value.status}{value.review_state ? ` · ${value.review_state}` : ''}</Text>
      <Text>{localized(value.payload.summary_plain ?? value.payload.description)}</Text>
      {value.payload.body && <Text>{localized(value.payload.body)}</Text>}
      <Text weight="semibold">Sources</Text>
      {value.sources.length === 0 && <Text muted>No sources listed for this revision.</Text>}
      {value.sources.map((source, index) => {
        const locator = source.selectors?.find(selector => selector.type === 'page')
        const page = source.page || locator?.label || locator?.value
        return (
          <Box key={index} padding="sm" border gap="xs" radius="sm">
            <Text>{source.object_name || source.object}{page ? ` · p. ${page}` : ''}</Text>
            {source.anchor && <Text muted>{source.anchor}</Text>}
            {source.quote && <Text>{source.quote}</Text>}
            <Text muted>{source.verdict || 'Unchecked'}</Text>
          </Box>
        )
      })}
    </Stack>
  )
}

function Workflows() {
  const selected = read(action)
  const error = read(workflowError)
  const prepared = read(prompt)
  const label = workflows.find(item => item.value === selected)?.argument || 'Details'
  return (
    <Stack gap="sm">
      <Select label="Workflow" value={selected} options={workflows.map(({ value, label }) => ({ value, label }))} onChange={value => {
        set(action, value)
        set(argument, '')
        set(prompt, '')
        set(workflowError, '')
      }} />
      <Input label={label} value={read(argument)} maxLength={1000} onChange={value => {
        set(argument, value)
        set(prompt, '')
        set(workflowError, '')
      }} />
      <Button onPress={() => {
        try {
          set(prompt, preparePrompt(read(action), read(argument)))
          set(workflowError, '')
        } catch (error) {
          set(workflowError, error instanceof Error ? error.message : String(error))
        }
      }}>Prepare prompt</Button>
      {error && <Callout tone="danger">{error}</Callout>}
      {prepared && (
        <Stack gap="xs">
          <Text muted>Copy this prompt into an Orca agent session with amemo MCP enabled.</Text>
          <CodeView code={prepared} label="Agent prompt" wrap />
        </Stack>
      )}
    </Stack>
  )
}

export default function Panel() {
  const value = read(record)
  const items = read(results)
  const connection = read(status)
  const busy = read(pending)
  const error = read(requestError)

  return (
    <Box padding="sm" gap="sm">
      {error && <Callout tone="danger">{error}</Callout>}
      <Tabs label="amemo">
        <Tab value="search" label="Search">
          {value ? <Record value={value} /> : (
            <Stack gap="sm">
              <Input label="Search memory" type="search" value={read(query)}
                placeholder="A term or record code" maxLength={500}
                onChange={text => set(query, text)} onSubmit={search} />
              <Button icon="search" disabled={busy || !read(query).trim()} onPress={() => search()}>Search</Button>
              {items?.length === 0 && <EmptyState title="No records found" icon="search" />}
              {!!items?.length && (
                <List label="Search results">
                  {items.map(item => (
                    <ListItem key={item.ref} title={item.label || item.ref} description={item.snippet}
                      disabled={busy} onPress={() => openRecord(item)}>
                      <Row gap="xs" wrap>
                        <Badge>{item.status}</Badge>
                        <Text muted>{item.review_state}</Text>
                      </Row>
                    </ListItem>
                  ))}
                </List>
              )}
            </Stack>
          )}
        </Tab>
        <Tab value="status" label="Status">
          <Stack gap="sm">
            <Button icon="refresh" disabled={busy} onPress={refreshStatus}>Check service</Button>
            {connection && <KeyValue items={[
              { label: 'Base', value: connection.base },
              { label: 'Semantic search', value: connection.semantic },
              { label: 'Queued for review', value: String(connection.queued) }
            ]} />}
          </Stack>
        </Tab>
        <Tab value="workflows" label="Agent">
          <Workflows />
        </Tab>
      </Tabs>
      {busy && <Spinner label="Waiting for amemo" />}
    </Box>
  )
}
