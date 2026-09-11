export function createStore(initial={}){
  let value=structuredClone(initial);const listeners=new Set();
  return {
    get:()=>value,
    set(patch){value={...value,...patch};for(const listener of listeners)listener(value);return value;},
    update(updater){value=updater(value);for(const listener of listeners)listener(value);return value;},
    subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener);}
  };
}
