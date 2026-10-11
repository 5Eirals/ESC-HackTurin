// Builds the control panel for Vercel from the board's dashboard (../assets), so there is one GUI:
// same index.html, app.js and design system; only libs/arduino.js changes, from the board's websocket to
// the cloud API (panel/arduino.js). Vercel runs it as the build command; output in public/.
import { copyFileSync, cpSync, existsSync, rmSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const assets = fileURLToPath(new URL('../../assets/', import.meta.url))
const out = fileURLToPath(new URL('../public/', import.meta.url))

if (!existsSync(assets)) {
  console.error(`Dashboard not found in ${assets}: on Vercel the Root Directory must be "cloud" with the`
    + ' "Include files outside the root directory" option on (the default).')
  process.exit(1)
}

rmSync(out, { recursive: true, force: true })
cpSync(assets, out, { recursive: true })
copyFileSync(fileURLToPath(new URL('../panel/arduino.js', import.meta.url)), `${out}libs/arduino.js`)
writeFileSync(`${out}libs/socket.io.min.js`, '// Not used in the cloud: the panel reads the Vercel API (libs/arduino.js).\n')
console.log(`Control panel built in ${out} from ${assets}`)
