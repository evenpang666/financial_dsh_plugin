import {rename} from 'node:fs/promises';
import {setTimeout as delay} from 'node:timers/promises';

// Windows readers and virus scanners can briefly hold the old destination open.
// Retain the original file until an atomic replacement succeeds.
export async function replaceFile(source, target) {
  for (let attempt = 0; ; attempt++) {
    try { return await rename(source, target); }
    catch (error) {
      if (attempt >= 5 || !['EPERM', 'EACCES', 'EBUSY'].includes(error.code)) throw error;
      await delay(20 * 2 ** attempt);
    }
  }
}
