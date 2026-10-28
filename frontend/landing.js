import * as THREE from 'three';
import {mergeGeometries} from 'three/addons/utils/BufferGeometryUtils.js';
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
// Turned wood profiles with continuous grain, used for the complete starting army.
const woodCanvas=document.createElement('canvas');woodCanvas.width=256;woodCanvas.height=512;
const wc=woodCanvas.getContext('2d'),pixels=wc.createImageData(256,512);
for(let y=0;y<512;y++)for(let x=0;x<256;x++){const wave=Math.sin(x*.23+Math.sin(y*.018)*3)+.4*Math.sin(x*.77+y*.012);const n=(Math.random()-.5)*9;const i=(y*256+x)*4;pixels.data[i]=132+wave*19+n;pixels.data[i+1]=78+wave*12+n;pixels.data[i+2]=41+wave*7+n;pixels.data[i+3]=255;}
wc.putImageData(pixels,0,0);const grain=new THREE.CanvasTexture(woodCanvas);grain.colorSpace=THREE.SRGBColorSpace;grain.wrapS=grain.wrapT=THREE.RepeatWrapping;
const wood=new THREE.MeshStandardMaterial({map:grain,color:0xe5bd89,roughness:.38,metalness:0,envMapIntensity:.7});
const ebony=new THREE.MeshStandardMaterial({map:grain,color:0x39282d,roughness:.36,metalness:0,envMapIntensity:.8});
function makePiece(type,mat){
 const group=new THREE.Group();const height={p:1.2,r:1.35,n:1.5,b:1.65,q:1.85,k:2}[type];
 const profile=[[0,0],[.33,0],[.36,.06],[.36,.13],[.30,.2],[.27,.24],[.24,.3],[.19,.4],[.14,.58],[.13,.72],[.22,.78],[.23,.84],[.14,1.05],[0,1.05]];
 const points=profile.map(([x,y])=>new THREE.Vector2(x,y*height*.75));
 const mesh=new THREE.Mesh(new THREE.LatheGeometry(points,32),mat);group.add(mesh);
 const topY=height*.75;
 if(type==='r'){const crown=new THREE.Mesh(new THREE.CylinderGeometry(.29,.25,.25,48),mat);crown.position.y=topY;group.add(crown);for(let i=0;i<6;i++){const t=new THREE.Mesh(new THREE.BoxGeometry(.12,.17,.12),mat);t.position.set(Math.cos(i*Math.PI/3)*.23,topY+.17,Math.sin(i*Math.PI/3)*.23);group.add(t);}}
 else if(type==='n'){const head=new THREE.Mesh(new THREE.ConeGeometry(.23,.55,4),mat);head.position.set(0,topY+.12,-.03);head.rotation.z=.25;group.add(head);const nose=new THREE.Mesh(new THREE.BoxGeometry(.18,.18,.3),mat);nose.position.set(0,topY+.18,-.15);group.add(nose);}
 else {const head=new THREE.Mesh(new THREE.SphereGeometry(type==='p'?.235:.2,24,16),mat);head.position.y=topY+.14;head.scale.y=type==='b'?1.6:1;group.add(head);if(type==='k'){for(const [w,h] of [[.08,.38],[.29,.08]]){const cross=new THREE.Mesh(new THREE.BoxGeometry(w,h,.08),mat);cross.position.y=topY+.54;group.add(cross);}}if(type==='q'){for(let i=0;i<7;i++){const bead=new THREE.Mesh(new THREE.SphereGeometry(.06,12,8),mat);bead.position.set(Math.cos(i*2*Math.PI/7)*.19,topY+.3,Math.sin(i*2*Math.PI/7)*.19);group.add(bead);}}}
 group.traverse(o=>{if(o.isMesh){o.castShadow=true;o.receiveShadow=true;}});return group;
}
const pawn=makePiece('p',wood.clone());scene.add(pawn);
const board=new THREE.Group();scene.add(board);
const base=new THREE.Mesh(new THREE.BoxGeometry(9,.28,9),new THREE.MeshStandardMaterial({color:0x101216,metalness:.4,roughness:.4}));base.position.y=-.19;board.add(base);
const tileGeo=new THREE.BoxGeometry(1,.045,1);
const light=new THREE.MeshStandardMaterial({color:0x9b7a61,roughness:.5});
const dark=new THREE.MeshStandardMaterial({color:0x352331,roughness:.5});
for(let x=0;x<8;x++)for(let z=0;z<8;z++){const tile=new THREE.Mesh(tileGeo,(x+z)%2?dark:light);tile.position.set(x-3.5,0,z-3.5);board.add(tile);}
const order=['r','n','b','q','k','b','n','r'];
for(let file=0;file<8;file++)for(const side of [0,1]){
const mat=side===0?wood:ebony;const major=makePiece(order[file],mat);major.position.set(file-3.5,.035,side===0?3.5:-3.5);if(side)major.rotation.y=Math.PI;board.add(major);
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
