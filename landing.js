import * as THREE from 'three';
import {mergeGeometries,toCreasedNormals} from 'three/addons/utils/BufferGeometryUtils.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
const canvas=document.querySelector('#scene');
const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
const clamp=(x)=>Math.max(0,Math.min(1,x));
const smooth=(a,b,x)=>{let t=clamp((x-a)/(b-a));return t*t*(3-2*t)};
const lerp=THREE.MathUtils.lerp;
try {
const renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:false,powerPreference:'high-performance'});
// Keep large displays from multiplying the scene into millions of extra pixels.
renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
renderer.shadowMap.autoUpdate=false;
renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.15;
const scene=new THREE.Scene();scene.fog=new THREE.FogExp2(0x08050d,.055);
const camera=new THREE.PerspectiveCamera(34,1,.1,100);
const pmrem=new THREE.PMREMGenerator(renderer);scene.environment=pmrem.fromScene(new RoomEnvironment(),.03).texture;pmrem.dispose();
scene.add(new THREE.HemisphereLight(0xdfe8f2,0x08050d,.32));
const key=new THREE.DirectionalLight(0xe9edf4,2.4);key.position.set(-3,7,5);key.castShadow=true;key.shadow.mapSize.set(1024,1024);key.shadow.camera.left=-10;key.shadow.camera.right=10;key.shadow.camera.top=10;key.shadow.camera.bottom=-10;key.shadow.bias=-.0004;key.shadow.normalBias=.03;scene.add(key);
const rim=new THREE.DirectionalLight(0xb49acb,1.0);rim.position.set(5,3,-4);scene.add(rim);
const fill=new THREE.DirectionalLight(0xffffff,.65);fill.position.set(3,0,6);scene.add(fill);
// Continuous wood grain, used for the complete starting army.
const woodCanvas=document.createElement('canvas');woodCanvas.width=256;woodCanvas.height=512;
const wc=woodCanvas.getContext('2d'),pixels=wc.createImageData(256,512);
for(let y=0;y<512;y++)for(let x=0;x<256;x++){const wave=Math.sin(x*.23+Math.sin(y*.018)*3)+.4*Math.sin(x*.77+y*.012);const n=(Math.random()-.5)*9;const i=(y*256+x)*4;pixels.data[i]=132+wave*19+n;pixels.data[i+1]=78+wave*12+n;pixels.data[i+2]=41+wave*7+n;pixels.data[i+3]=255;}
wc.putImageData(pixels,0,0);const grain=new THREE.CanvasTexture(woodCanvas);grain.colorSpace=THREE.SRGBColorSpace;grain.wrapS=grain.wrapT=THREE.RepeatWrapping;
const wood=new THREE.MeshStandardMaterial({map:grain,color:0xe5bd89,roughness:.38,metalness:0,envMapIntensity:.7});
const ebony=new THREE.MeshStandardMaterial({map:grain,color:0x39282d,roughness:.36,metalness:0,envMapIntensity:.8});
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
 // STL has no texture coordinates: project the grain straight through, as if cut from one block.
 const p=geometry.attributes.position,uv=new Float32Array(p.count*2);
 for(let i=0;i<p.count;i++){uv[i*2]=(p.getX(i)+p.getZ(i)*.6)*.55;uv[i*2+1]=p.getY(i)*.25;}
 geometry.setAttribute('uv',new THREE.BufferAttribute(uv,2));
 return geometry;
}
const models=Object.fromEntries(await Promise.all(Object.entries(modelNames).map(async([type,name])=>[type,await loadModel(name)])));
function makePiece(type,mat){const mesh=new THREE.Mesh(models[type],mat);mesh.castShadow=true;mesh.receiveShadow=true;return mesh;}
const pawn=makePiece('p',wood.clone());scene.add(pawn);
const board=new THREE.Group();scene.add(board);
const base=new THREE.Mesh(new THREE.BoxGeometry(9,.28,9),new THREE.MeshStandardMaterial({color:0x101216,metalness:.4,roughness:.4}));base.position.y=-.19;board.add(base);
const tileGeo=new THREE.BoxGeometry(1,.045,1);
const light=new THREE.MeshStandardMaterial({color:0x9b7a61,roughness:.5});
const dark=new THREE.MeshStandardMaterial({color:0x352331,roughness:.5});
for(let x=0;x<8;x++)for(let z=0;z<8;z++){const tile=new THREE.Mesh(tileGeo,(x+z)%2?dark:light);tile.position.set(x-3.5,0,z-3.5);board.add(tile);}
const order=['r','n','b','q','k','b','n','r'];
for(let file=0;file<8;file++)for(const side of [0,1]){
// The flat knight model's muzzle points along +X: turn knights inward so the camera sees their profile, not their edge.
const mat=side===0?wood:ebony;const major=makePiece(order[file],mat);major.position.set(file-3.5,.035,side===0?3.5:-3.5);if(order[file]==='n'?file>3:side)major.rotation.y=Math.PI;board.add(major);
if(!(side===0&&file===4)){const p=makePiece('p',mat);p.position.set(file-3.5,.035,side===0?2.5:-2.5);board.add(p);}}
board.updateMatrixWorld(true);const batches=new Map();
board.traverse(o=>{if(!o.isMesh)return;const g=o.geometry.clone().applyMatrix4(o.matrixWorld);if(!batches.has(o.material))batches.set(o.material,[]);batches.get(o.material).push(g);});board.clear();
for(const [material,geometries] of batches){const mesh=new THREE.Mesh(mergeGeometries(geometries),material);mesh.receiveShadow=true;board.add(mesh);geometries.forEach(g=>g.dispose());}
const boardMaterials=[...batches.keys()];boardMaterials.forEach(m=>m.transparent=true);
const floor=new THREE.Mesh(new THREE.PlaneGeometry(100,100),new THREE.MeshStandardMaterial({color:0x090510,roughness:1,envMapIntensity:.05}));floor.rotation.x=-Math.PI/2;floor.position.y=-.37;floor.receiveShadow=true;scene.add(floor);
// Atmosphere is a quiet CSS backdrop; no full-screen transparent smoke passes.
const intro=document.querySelector('.intro'),descent=document.querySelector('.descent'),arrival=document.querySelector('.arrival');
const masthead=document.querySelector('.masthead');
const spotlight=new THREE.SpotLight(0xffe3c5,65,30,.38,.85,1.5);
spotlight.position.set(1.5,10,5);scene.add(spotlight);scene.add(spotlight.target);
const journey=document.querySelector('.journey');
let progress=0,target=0,journeyStart=0,scrollRange=1,stageHeight=1;
let frameHandle=0,previous=0,spinSeconds=0,lastPose=-1,lastUI=-1;
let dirty=true,ready=false,inView=true;
let frameCount=0,frameWindow=0,renderedFrames=0,shadowUpdates=0;

function pause(reason){
 if(frameHandle)cancelAnimationFrame(frameHandle);
 frameHandle=0;previous=0;frameCount=0;frameWindow=0;
 canvas.dataset.renderState=reason;canvas.dataset.fps='0';
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
 const ratio=Math.min(devicePixelRatio,1.25,Math.sqrt(2000000/(width*height)));
 renderer.setPixelRatio(ratio);renderer.setSize(width,height,false);
 camera.aspect=width/height;camera.updateProjectionMatrix();
 stageHeight=height;journeyStart=journey.getBoundingClientRect().top+scrollY;
 scrollRange=Math.max(1,journey.offsetHeight-height);
 canvas.dataset.pixelRatio=ratio.toFixed(2);
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
 progress=reduced?target:lerp(progress,target,1-Math.exp(-14*dt));
 if(Math.abs(progress-target)<.00001)progress=target;
 const p=progress,mobile=innerWidth<700;
 const drop=smooth(.3,.65,p),advance=smooth(.7,.94,p),reveal=smooth(.22,.43,p);
 const spinning=!reduced&&drop<1;
 const changed=dirty||Math.abs(p-lastPose)>.000001;
 const settling=progress!==target;
 if(!changed&&!spinning){pause('idle');return;}
 if(spinning)spinSeconds+=dt;
 board.visible=reveal>.005;floor.visible=board.visible;
 for(const material of boardMaterials){
   const transparent=reveal<1;
   if(material.transparent!==transparent){material.transparent=transparent;material.needsUpdate=true;}
   material.opacity=reveal;
 }
 pawn.position.set(.5,lerp(5,.035,drop),2.5-advance*2);
 pawn.rotation.set(lerp(.24,0,drop),reduced?0:(spinSeconds*.35+p*5)*(1-drop),lerp(-.28,0,drop));
 pawn.scale.setScalar(lerp(1.55,1,drop));spotlight.target.position.copy(pawn.position);
 const chase=smooth(.3,.68,p);
 camera.position.set(lerp(.5,1.5,chase),lerp(5.7,2.3,chase),lerp(mobile?12:10,6.3,chase)-advance*2);
 camera.lookAt(.5,lerp(5.8,.85,chase),lerp(.5,1.7,chase)-advance*2);
 if(dirty||Math.abs(p-lastUI)>.00001){
   setChapter(intro,smooth(.08,.18,p)*(1-smooth(.29,.38,p)));
   setChapter(descent,smooth(.4,.48,p)*(1-smooth(.6,.68,p)));
   setChapter(arrival,smooth(.92,1,p));
   masthead.classList.toggle('visible',p>.08);lastUI=p;
 }
 // Do not switch shadow rendering on/off mid-scroll (that changes shader variants).
 // Only rebuild the shadow when the piece or board visibility actually changes.
 renderer.shadowMap.needsUpdate=board.visible&&(changed||spinning);
 if(renderer.shadowMap.needsUpdate)shadowUpdates++;
 renderer.render(scene,camera);
 dirty=false;lastPose=p;renderedFrames++;
 canvas.dataset.renderState=spinning?'spinning':'scrolling';
 canvas.dataset.progress=p.toFixed(4);
 canvas.dataset.frames=String(renderedFrames);
 canvas.dataset.shadowUpdates=String(shadowUpdates);
 canvas.dataset.drawCalls=String(renderer.info.render.calls);
 canvas.dataset.triangles=String(renderer.info.render.triangles);
 if(!frameWindow)frameWindow=now;
 frameCount++;
 if(now-frameWindow>1000){canvas.dataset.fps=String(Math.round(frameCount*1000/(now-frameWindow)));frameCount=0;frameWindow=now;}
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
canvas.dataset.shaderWarmupMs=String(Math.round(performance.now()-warmupStart));
ready=true;requestFrame();
} catch(error){document.querySelector('#scene-fallback').hidden=false;console.error(error);}
