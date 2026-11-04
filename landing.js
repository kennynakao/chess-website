import * as THREE from 'three';
import {mergeGeometries,mergeVertices,toCreasedNormals} from 'three/addons/utils/BufferGeometryUtils.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
const canvas=document.querySelector('#scene');
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp=(x)=>Math.max(0,Math.min(1,x));
const smooth=(a,b,x)=>{let t=clamp((x-a)/(b-a));return t*t*(3-2*t)};
const lerp=THREE.MathUtils.lerp;
// The DUEN colors: gold and near-black pieces on a cream and purple board.
const theme={accent:0xf4c040,fog:0x33205a,sky:0xfff1d6,ground:0x33205a,whiteSet:{color:0xf4c040,roughness:.3,metalness:.65,envMapIntensity:1.2},blackSet:{color:0x120a1e,roughness:.3,metalness:.5,envMapIntensity:1.3},slab:0x241440,light:0xfcecb8,dark:0x442074};
try {
const renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:true,powerPreference:'high-performance'});
// Keep large displays from multiplying the scene into millions of extra pixels.
renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
renderer.shadowMap.autoUpdate=false;
renderer.toneMapping=THREE.NeutralToneMapping;renderer.toneMappingExposure=1;
const scene=new THREE.Scene();scene.fog=new THREE.FogExp2(theme.fog,.014);
const camera=new THREE.PerspectiveCamera(34,1,.1,100);
const pmrem=new THREE.PMREMGenerator(renderer);scene.environment=pmrem.fromScene(new RoomEnvironment(),.03).texture;scene.environmentIntensity=.62;pmrem.dispose();
scene.add(new THREE.HemisphereLight(theme.sky,theme.ground,.25));
const key=new THREE.DirectionalLight(0xffffff,1.2);key.position.set(-3,9,5);key.castShadow=true;key.shadow.mapSize.set(1024,1024);key.shadow.camera.left=-10;key.shadow.camera.right=10;key.shadow.camera.top=10;key.shadow.camera.bottom=-10;key.shadow.bias=-.0004;key.shadow.normalBias=.03;scene.add(key);
const rim=new THREE.DirectionalLight(0xffffff,.35);rim.position.set(5,3,-4);scene.add(rim);
const whiteSet=new THREE.MeshStandardMaterial(theme.whiteSet);
const blackSet=new THREE.MeshStandardMaterial(theme.blackSet);
// The chess piece models: binary STL in millimetres, Y up. One scale keeps their true proportions.
const MODEL_SCALE=.028,modelNames={p:'pawn',n:'knight',b:'bishop',r:'rook',q:'queen',k:'king'};
async function loadModel(name){
 const response=await fetch(`assets/models/${name}.stl`);
 if(!response.ok)throw new Error(`Could not load the ${name} model`);
 const data=new DataView(await response.arrayBuffer()),count=data.getUint32(80,true),positions=new Float32Array(count*9);
 for(let t=0;t<count;t++)for(let v=0;v<9;v++)positions[t*9+v]=data.getFloat32(96+t*50+v*4,true);
 const raw=new THREE.BufferGeometry();raw.setAttribute('position',new THREE.BufferAttribute(positions,3));
 // Smooth curves but keep sharp edges. This hashes vertex positions, so it runs before scaling down.
 const geometry=toCreasedNormals(raw,Math.PI/6);
 geometry.computeBoundingBox();const {min,max}=geometry.boundingBox;
 geometry.translate(-(min.x+max.x)/2,-min.y,-(min.z+max.z)/2);geometry.scale(MODEL_SCALE,MODEL_SCALE,MODEL_SCALE);
 // Share vertices between triangles: the army draws with far fewer vertex shader runs.
 return mergeVertices(geometry);
}
const models=Object.fromEntries(await Promise.all(Object.entries(modelNames).map(async([type,name])=>[type,await loadModel(name)])));
function makePiece(type,mat){const mesh=new THREE.Mesh(models[type],mat);mesh.castShadow=true;mesh.receiveShadow=true;return mesh;}
const pawn=makePiece('p',whiteSet.clone());scene.add(pawn);
const board=new THREE.Group();scene.add(board);
const slab=new THREE.MeshStandardMaterial({color:theme.slab,metalness:.1,roughness:.45});
const base=new THREE.Mesh(new THREE.BoxGeometry(9,.28,9),slab);base.position.y=-.19;board.add(base);
const tileGeo=new THREE.BoxGeometry(1,.045,1);
const light=new THREE.MeshStandardMaterial({color:theme.light,roughness:.5});
const dark=new THREE.MeshStandardMaterial({color:theme.dark,roughness:.5});
for(let x=0;x<8;x++)for(let z=0;z<8;z++){const tile=new THREE.Mesh(tileGeo,(x+z)%2?dark:light);tile.position.set(x-3.5,0,z-3.5);board.add(tile);}
// Soft contact shadows: one merged quad per piece instead of a shadow pass over the whole army.
const shadowCanvas=document.createElement('canvas');shadowCanvas.width=shadowCanvas.height=128;
const sc=shadowCanvas.getContext('2d'),falloff=sc.createRadialGradient(64,64,0,64,64,64);
falloff.addColorStop(0,'rgba(0,0,0,.6)');falloff.addColorStop(.5,'rgba(0,0,0,.42)');falloff.addColorStop(1,'rgba(0,0,0,0)');
sc.fillStyle=falloff;sc.fillRect(0,0,128,128);
const contact=new THREE.MeshBasicMaterial({map:new THREE.CanvasTexture(shadowCanvas),transparent:true,depthWrite:false,toneMapped:false,fog:false,opacity:0});
const contactQuad=(x,z)=>new THREE.PlaneGeometry(1.1,1.1).rotateX(-Math.PI/2).translate(x,.03,z);
const contactQuads=[];
const order=['r','n','b','q','k','b','n','r'];
for(let file=0;file<8;file++)for(const side of [0,1]){
// The flat knight model's muzzle points along +X: turn knights inward so the camera sees their profile, not their edge.
const mat=side===0?whiteSet:blackSet;const major=makePiece(order[file],mat);major.position.set(file-3.5,.035,side===0?3.5:-3.5);if(order[file]==='n'?file>3:side)major.rotation.y=Math.PI;board.add(major);contactQuads.push(contactQuad(file-3.5,side===0?3.5:-3.5));
if(!(side===0&&file===4)){const p=makePiece('p',mat);p.position.set(file-3.5,.035,side===0?2.5:-2.5);board.add(p);contactQuads.push(contactQuad(file-3.5,side===0?2.5:-2.5));}}
board.updateMatrixWorld(true);const batches=new Map();
board.traverse(o=>{if(!o.isMesh)return;const g=o.geometry.clone().applyMatrix4(o.matrixWorld);if(!batches.has(o.material))batches.set(o.material,[]);batches.get(o.material).push(g);});board.clear();
for(const [material,geometries] of batches){const mesh=new THREE.Mesh(mergeGeometries(geometries),material);mesh.receiveShadow=material===light||material===dark||material===slab;mesh.castShadow=material===slab;board.add(mesh);geometries.forEach(g=>g.dispose());}
const boardMaterials=[...batches.keys()];boardMaterials.forEach(m=>m.transparent=true);
board.add(new THREE.Mesh(mergeGeometries(contactQuads),contact));
const pawnContact=new THREE.Mesh(contactQuad(0,0),contact.clone());scene.add(pawnContact);
// The move itself, in the page's accent color: the two squares the pawn travels between.
const moveMark=new THREE.Mesh(mergeGeometries([.5,2.5].map(z=>new THREE.PlaneGeometry(1,1).rotateX(-Math.PI/2).translate(.5,.026,z))),new THREE.MeshBasicMaterial({color:theme.accent,transparent:true,depthWrite:false,toneMapped:false,fog:false,opacity:0}));moveMark.renderOrder=-1;scene.add(moveMark);
// Only the shadow is drawn, so the board sits straight on the page with no horizon line.
const floor=new THREE.Mesh(new THREE.PlaneGeometry(100,100),new THREE.ShadowMaterial({opacity:0}));floor.rotation.x=-Math.PI/2;floor.position.y=-.37;floor.receiveShadow=true;scene.add(floor);
const intro=document.querySelector('.intro'),descent=document.querySelector('.descent'),arrival=document.querySelector('.arrival');
const journey=document.querySelector('.journey');
let progress=0,target=0,journeyStart=0,scrollRange=1,stageHeight=1;
const SPIN_AT_REST=.35,SPIN_SCROLLING=4; // Radians per second; 4 is one full turn in about 1.6 seconds.
let frameHandle=0,previous=0,spinAngle=0,spinRate=reduced?0:SPIN_AT_REST,lastPose=-1,lastUI=-1;
let dirty=true,ready=false,inView=true;
// Slow device: trade canvas resolution for smooth scrolling. Text stays sharp.
let quality=1,slowFrames=0;
const debug=new URLSearchParams(location.search).has('debug');
let frameCount=0,frameWindow=0,renderedFrames=0,shadowUpdates=0;

function pause(reason){
 if(frameHandle)cancelAnimationFrame(frameHandle);
 frameHandle=0;previous=0;frameCount=0;frameWindow=0;
 if(debug){canvas.dataset.renderState=reason;canvas.dataset.fps='0';}
}
function requestFrame(){
 if(ready&&!document.hidden&&inView&&!frameHandle)frameHandle=requestAnimationFrame(frame);
}
function update(){
 const offset=scrollY-journeyStart;
 target=clamp(offset/scrollRange);
 inView=offset<scrollRange+stageHeight&&offset+stageHeight>0;
 if(inView)requestFrame();else pause('offscreen');
}
function resize(){
 const width=innerWidth,height=innerHeight;
 // Cap the drawing buffer, not the DOM or text resolution.
 const ratio=Math.min(devicePixelRatio,1.25,Math.sqrt(2000000/(width*height)))*quality;
 renderer.setPixelRatio(ratio);renderer.setSize(width,height,false);
 camera.aspect=width/height;camera.updateProjectionMatrix();
 stageHeight=height;journeyStart=journey.getBoundingClientRect().top+scrollY;
 scrollRange=Math.max(1,journey.offsetHeight-height);
 dirty=true;update();
}
function setChapter(element,opacity){
 element.style.opacity=opacity.toFixed(4);
 element.style.visibility=opacity<.01?'hidden':'visible';
}
function frame(now){
 frameHandle=0;
 if(document.hidden||!inView){pause(document.hidden?'hidden':'offscreen');return;}
 const dt=previous?Math.min((now-previous)/1000,.05):1/60;previous=now;
 slowFrames=dt>.028?slowFrames+1:Math.max(0,slowFrames-1);
 if(slowFrames>20&&quality>.6){quality-=.2;slowFrames=0;resize();}
 progress=reduced?target:lerp(progress,target,1-Math.exp(-20*dt));
 if(Math.abs(progress-target)<.00001)progress=target;
 const p=progress,mobile=innerWidth<700;
 const drop=smooth(.3,.65,p),advance=smooth(.7,.94,p),reveal=smooth(.22,.43,p);
 const settling=progress!==target;
 // The spin runs on the clock, never the scroll distance: one fixed speed while scrolling, however fast,
 // and a slow one at rest. It eases between them, and to a stop once the pawn lands.
 if(!reduced){spinRate=lerp(spinRate,drop<1?(settling?SPIN_SCROLLING:SPIN_AT_REST):0,1-Math.exp(-5*dt));if(drop>=1&&spinRate<.005)spinRate=0;}
 const spinning=spinRate>0;
 const changed=dirty||Math.abs(p-lastPose)>.000001;
 if(!changed&&!spinning){pause('idle');return;}
 spinAngle+=spinRate*dt;
 board.visible=reveal>.005;floor.visible=board.visible;
 for(const material of boardMaterials){
   const transparent=reveal<1;
   if(material.transparent!==transparent){material.transparent=transparent;material.needsUpdate=true;}
   material.opacity=reveal;
 }
 contact.opacity=reveal;floor.material.opacity=.22*reveal;
 moveMark.material.opacity=.9*smooth(.68,.82,p);moveMark.visible=moveMark.material.opacity>0;
 pawn.position.set(.5,lerp(5,.035,drop),2.5-advance*2);
 // A soft shadow grounds the floating pawn, then becomes its contact shadow on the board.
 const hover=1-smooth(.26,.36,p);
 pawnContact.position.set(.5,hover>0?pawn.position.y-.28:0,pawn.position.z);pawnContact.scale.setScalar(hover>0?1.9:1);
 pawnContact.material.opacity=Math.max(hover*.3,reveal*smooth(.45,.65,p));
 pawn.rotation.set(lerp(.24,0,drop),spinAngle,lerp(-.28,0,drop));
 pawn.scale.setScalar(lerp(1.9,1,drop));
 const chase=smooth(.3,.68,p);
 // High enough to see the pawn's square over White's back rank, with the whole board below the headline.
 // Off to one side, so the pawn shows between the white king and bishop rather than behind the king.
 const [height,distance,aim]=mobile?[9,17,2.1]:[8.5,19,2.46];
 camera.position.set(lerp(.5,2.6,chase),lerp(5.7,height,chase),lerp(mobile?12:10,distance,chase)-advance*2);
 camera.lookAt(.5,lerp(6.75,aim,chase),lerp(.5,1.5,chase)-advance*2);
 if(dirty||Math.abs(p-lastUI)>.00001){
   setChapter(intro,1-smooth(.2,.3,p));
   setChapter(descent,smooth(.4,.48,p)*(1-smooth(.6,.68,p)));
   setChapter(arrival,smooth(.92,1,p));
   lastUI=p;
 }
 // Do not switch shadow rendering on/off mid-scroll (that changes shader variants).
 // Only rebuild the shadow when the piece or board visibility actually changes.
 renderer.shadowMap.needsUpdate=board.visible&&(changed||spinning);
 if(renderer.shadowMap.needsUpdate)shadowUpdates++;
 renderer.render(scene,camera);
 dirty=false;lastPose=p;renderedFrames++;
 if(debug){
  canvas.dataset.renderState=spinning?'spinning':'scrolling';
  canvas.dataset.progress=p.toFixed(4);
  canvas.dataset.spinAngle=spinAngle.toFixed(4);
  canvas.dataset.frames=String(renderedFrames);
  canvas.dataset.shadowUpdates=String(shadowUpdates);
  canvas.dataset.drawCalls=String(renderer.info.render.calls);
  canvas.dataset.triangles=String(renderer.info.render.triangles);
  canvas.dataset.pixelRatio=renderer.getPixelRatio().toFixed(2);
  if(!frameWindow)frameWindow=now;
  frameCount++;
  if(now-frameWindow>1000){canvas.dataset.fps=String(Math.round(frameCount*1000/(now-frameWindow)));frameCount=0;frameWindow=now;}
 }
 if(spinning||settling)requestFrame();else pause('idle');
}
addEventListener('resize',resize);
addEventListener('scroll',update,{passive:true});
document.addEventListener('visibilitychange',()=>{if(document.hidden)pause('hidden');else{dirty=true;update();}});
addEventListener('pagehide',()=>pause('hidden'));
addEventListener('pageshow',()=>{dirty=true;update();});
resize();
// Prepare both reveal and opaque materials before the first scroll into the board.
// Keeping the light/shadow configuration stable avoids first-reveal shader stalls.
const warmupStart=performance.now();
camera.position.set(.5,5.7,10);camera.lookAt(.5,5.8,.5);
await renderer.compileAsync(scene,camera);
for(const material of boardMaterials){material.transparent=false;material.needsUpdate=true;}
await renderer.compileAsync(scene,camera);
// Upload the whole army now: otherwise its first appearance stalls the scroll.
board.visible=floor.visible=true;camera.position.set(1.2,3.7,13.5);camera.lookAt(.5,1.75,0);
renderer.render(scene,camera);renderer.clear();board.visible=floor.visible=false;
if(debug)canvas.dataset.shaderWarmupMs=String(Math.round(performance.now()-warmupStart));
ready=true;requestFrame();
} catch(error){document.querySelector('#scene-fallback').hidden=false;console.error(error);}
