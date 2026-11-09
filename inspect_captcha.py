"""
inspect_captcha.py — one-off: dump the login CAPTCHA area so we can target the
DIGITS element precisely (collect_captcha.py grabbed the refresh icon instead).

    C:\\Python38\\python.exe inspect_captcha.py --code bkv

Attaches to a VHP window, opens the login page, finds the 'Enter CAPTCHA' box, and
prints the surrounding HTML plus every nearby image/canvas/svg/div with its size.
Send me EVERYTHING it prints. Nothing is uploaded or submitted.
"""
import argparse
import sys
import time

import config
from browser import kill_stale_chromedrivers, make_driver_attached
from runlock import RunLock

LOGIN_URL = "https://e1-vhp.com/login"

# JS: locate the CAPTCHA input, climb to a container, and report that container's
# HTML + every child img/canvas/svg/div/i with tag/class/id/size/position, so the
# wide digit box is easy to tell apart from the small square refresh icon.
_DUMP_JS = r"""
function info(e){
  var r = e.getBoundingClientRect();
  return {tag:e.tagName, cls:(e.getAttribute&&e.getAttribute('class'))||'',
          id:e.id||'', w:Math.round(r.width), h:Math.round(r.height),
          x:Math.round(r.left), y:Math.round(r.top),
          src:((e.getAttribute&&e.getAttribute('src'))||'').slice(0,80)};
}
var inp=null, ins=document.querySelectorAll('input');
for(var i=0;i<ins.length;i++){
  var ph=(ins[i].getAttribute('placeholder')||'').toLowerCase();
  if(ph.indexOf('captcha')>=0){ inp=ins[i]; break; }
}
var out={input: inp?info(inp):null, container:'', candidates:[]};
if(inp){
  var box=inp;
  for(var k=0;k<4 && box.parentElement;k++){ box=box.parentElement; }
  out.container=(box.outerHTML||'').replace(/\s+/g,' ').slice(0,3500);
  var els=box.querySelectorAll('img,canvas,svg,div,i,span,button');
  for(var j=0;j<els.length;j++){
    var r=els[j].getBoundingClientRect();
    if(r.width>=12 && r.height>=10 && r.width<500 && r.height<160){
      out.candidates.push(info(els[j]));
    }
  }
}
return JSON.stringify(out, null, 1);
"""


def main(argv):
    parser = argparse.ArgumentParser(description="Dump the login CAPTCHA area")
    parser.add_argument("--code", default="bkv", help="which VHP window: bkds/bkdu/bkv")
    parser.add_argument("--url", default=LOGIN_URL)
    args = parser.parse_args(argv)

    prop = next((p for p in config.PROPERTIES if p["code"].lower() == args.code.lower()), None)
    if prop is None:
        print("no property {!r} (use bkds/bkdu/bkv)".format(args.code))
        return 2

    lock = RunLock(config.RUN_LOCK_PATH)
    if not lock.acquire(wait=120):
        print("another VHP run is using Chrome right now — try again in a minute")
        return 1
    try:
        kill_stale_chromedrivers()
        print("Attaching to {} (port {}) ...".format(prop["code"].upper(), prop["debug_port"]))
        driver = make_driver_attached(prop["debug_port"], config.DOWNLOADS_DIR)
        driver.get(args.url)
        time.sleep(2.0)

        print("\n===== what collect_captcha.py currently grabs =====")
        try:
            from captcha import _find_captcha_image
            el = _find_captcha_image(driver)
            if el is None:
                print("(_find_captcha_image found nothing)")
            else:
                print("tag={} class={!r} size={}".format(
                    el.tag_name, el.get_attribute("class"), el.size))
        except Exception as exc:  # noqa: BLE001
            print("(error: {})".format(exc))

        print("\n===== CAPTCHA area dump (send me ALL of this) =====")
        try:
            print(driver.execute_script(_DUMP_JS))
        except Exception as exc:  # noqa: BLE001
            print("(dump failed: {})".format(exc))
        print("\n----- done. Copy everything above and send it to me. -----")
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
