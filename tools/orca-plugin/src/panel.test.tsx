import { assert, mockHost, mount, test } from 'orca/testing'
import * as panel from './panel'

test('opens the exact search revision and renders translated text and the source quote', async () => {
  const ref = 'atm:00000000-0000-0000-0000-000000000001'
  const host = mockHost({
    requests: {
      'amemo.search': ({ query }) => {
        assert.equal(query, 'bearing')
        return [{ ref, revision: 2, label: 'Bearing', snippet: 'Result', status: 'verified', review_state: 'none' }]
      },
      'amemo.record': (input) => {
        assert.deepEqual(input, { ref, revision: 2 })
        return {
          ref, revision: 2, status: 'verified',
          payload: { label: { en: 'Bearing size' }, summary_plain: { en: 'Passed inspection' } },
          sources: [{ object: 'sample', object_name: 'report.pdf', page: 3, quote: 'Measured 12 mm', verdict: 'ok' }]
        }
      }
    }
  })

  const view = await mount(panel, { host })
  try {
    await view.type({ label: 'Search memory' }, 'bearing', { submit: true })
    assert.ok(view.find({ type: 'ListItem' }), JSON.stringify(view.tree()))
    await view.press({ type: 'ListItem' })
    assert.ok(view.find({ text: 'Bearing size' }))
    assert.ok(view.find({ text: 'Measured 12 mm' }))
    assert.deepEqual(view.errors, [])
    assert.deepEqual(view.warnings, [])
  } finally {
    await view.unmount()
  }
})
