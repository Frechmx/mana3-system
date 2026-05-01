// --- GSAP TYPOGRAPHY ANIMATION ---
document.addEventListener("DOMContentLoaded", () => {
    gsap.fromTo(".fade-up", 
        { y: 30, opacity: 0, filter: "blur(8px)" }, 
        { y: 0, opacity: 1, filter: "blur(0px)", duration: 1.5, stagger: 0.15, ease: "power3.out", delay: 0.2 }
    );
});

// --- THREE.JS CUSTOM SHADER ENGINE ---
const container = document.getElementById('canvas-container');

const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2('#0A0A0A', 0.12);

const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 100);
camera.position.z = 8.5;
camera.position.y = 0.2;

const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
container.appendChild(renderer.domElement);

function createGlowTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = 32; canvas.height = 32;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createRadialGradient(16, 16, 0, 16, 16, 16);
    gradient.addColorStop(0, 'rgba(255, 255, 255, 1)');
    gradient.addColorStop(0.3, 'rgba(255, 255, 255, 0.5)');
    gradient.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 32, 32);
    return new THREE.CanvasTexture(canvas);
}

function createVaporTexture() {
    const canvas = document.createElement('canvas');
    canvas.width = 512; canvas.height = 512;
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createRadialGradient(256, 256, 0, 256, 256, 256); 
    gradient.addColorStop(0, 'rgba(245, 245, 240, 0)'); 
    gradient.addColorStop(0.10, 'rgba(245, 245, 240, 0)'); 
    gradient.addColorStop(0.115, 'rgba(245, 245, 240, 0.8)'); 
    gradient.addColorStop(0.25, 'rgba(58, 134, 255, 0.25)'); 
    gradient.addColorStop(1, 'rgba(58, 134, 255, 0)'); 
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 512, 512);
    return new THREE.CanvasTexture(canvas);
}

const particleTexture = createGlowTexture();
const vaporTexture = createVaporTexture();

// --- SCENE 1: THE SINGULARITY (Y = 0) ---
const masterGroup = new THREE.Group();
scene.add(masterGroup);

const scrollGroup = new THREE.Group();
scrollGroup.rotation.y = Math.PI / 5; 
masterGroup.add(scrollGroup);

const breathingGroup = new THREE.Group();
scrollGroup.add(breathingGroup);

const eventHorizonRadius = 0.40;
const voidMat = new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true }); 
breathingGroup.add(new THREE.Mesh(new THREE.SphereGeometry(eventHorizonRadius, 32, 32), voidMat));

const rimShader = {
    vertexShader: `varying vec3 vNormal; void main() { vNormal = normalize(normalMatrix * normal); gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `varying vec3 vNormal; uniform float uOpacity; void main() { float intensity = pow(0.65 - dot(vNormal, vec3(0, 0, 1.0)), 4.0); gl_FragColor = vec4(0.6, 0.8, 1.0, 1.0) * intensity * uOpacity; }`
};
const rimMat = new THREE.ShaderMaterial({ vertexShader: rimShader.vertexShader, fragmentShader: rimShader.fragmentShader, uniforms: { uOpacity: { value: 0.7 } }, blending: THREE.AdditiveBlending, transparent: true, depthWrite: false });
breathingGroup.add(new THREE.Mesh(new THREE.SphereGeometry(eventHorizonRadius + 0.015, 32, 32), rimMat));

const vaporMat = new THREE.MeshBasicMaterial({ map: vaporTexture, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, opacity: 0.8 });
const vaporRing = new THREE.Mesh(new THREE.PlaneGeometry(7, 7), vaporMat);
vaporRing.rotation.y = Math.PI / 2; vaporRing.scale.set(1, 1.5, 1); 
breathingGroup.add(vaporRing);

const innerRingMat = new THREE.MeshBasicMaterial({ color: 0xF5F5F0, transparent: true, opacity: 0.8, blending: THREE.AdditiveBlending });
const innerRing = new THREE.Mesh(new THREE.TorusGeometry(0.48, 0.004, 16, 100), innerRingMat);
innerRing.rotation.y = Math.PI / 2;
breathingGroup.add(innerRing);

const vertexShader = `
    attribute float aLayerId; attribute vec3 aBaseColor; attribute vec3 aSignalColor; attribute float aBaseAngle; attribute float aOffsetAngle; 
    uniform float uTime; uniform float uInteractionMode; uniform float uActiveLayer; uniform float uPixelRatio;
    varying vec3 vColor;
    void main() {
        vec3 separatedPos = vec3(cos(aBaseAngle + uTime * 0.1) * (1.2 + aLayerId * 0.15), (aLayerId - 1.5) * 0.7, sin(aBaseAngle + uTime * 0.1) * (1.2 + aLayerId * 0.15));
        vec3 transformed = mix(position, separatedPos, uInteractionMode);
        transformed.x += sin(uTime * 0.5 + aOffsetAngle) * 0.05; transformed.y += cos(uTime * 0.5 + aOffsetAngle) * 0.05;
        vColor = aBaseColor;
        if (uInteractionMode > 0.5 && uActiveLayer > -0.5) {
            if (abs(uActiveLayer - aLayerId) < 0.1) { vColor = aSignalColor; transformed.y += sin(uTime * 4.0) * 0.05; } else { vColor = aBaseColor * 0.15; }
        }
        vec4 mvPosition = modelViewMatrix * vec4(transformed, 1.0); gl_Position = projectionMatrix * mvPosition;
        float baseSize = (18.0 * uPixelRatio) / -mvPosition.z;
        gl_PointSize = (uInteractionMode > 0.5 && uActiveLayer > -0.5 && abs(uActiveLayer - aLayerId) < 0.1) ? baseSize * 2.0 : baseSize;
    }
`;
const fragmentShader = `uniform sampler2D uTexture; varying vec3 vColor; void main() { gl_FragColor = texture2D(uTexture, gl_PointCoord); gl_FragColor.rgb *= vColor; }`;

const particleCount = 10000;
const geometry = new THREE.BufferGeometry();
const positions = new Float32Array(particleCount * 3), layerIds = new Float32Array(particleCount), baseColors = new Float32Array(particleCount * 3), signalColors = new Float32Array(particleCount * 3), offsetAngles = new Float32Array(particleCount), baseAngles = new Float32Array(particleCount);
const colorBone = new THREE.Color('#F5F5F0'), colorBlue = new THREE.Color('#3A86FF'), colorYellow = new THREE.Color('#FFD60A'), colorRed = new THREE.Color('#FF0033'), colorGreen = new THREE.Color('#38B000');
const subduedColor = colorBone.clone().multiplyScalar(0.6);
const signals = [colorBlue, colorYellow, colorRed, colorGreen];

for (let i = 0; i < particleCount; i++) {
    const layerIdx = Math.floor(Math.random() * 4); layerIds[i] = layerIdx;
    let x = Math.random() * 8; x = (Math.random() > 0.5) ? -x : x;
    const radius = Math.pow(Math.abs(x), 0.6) * 1.3 + (eventHorizonRadius + 0.02);
    const angle = Math.random() * Math.PI * 2;
    positions[i*3] = x; positions[i*3+1] = Math.cos(angle) * radius; positions[i*3+2] = Math.sin(angle) * radius; 
    if (Math.random() > 0.80) { const c = signals[layerIdx]; baseColors[i*3] = c.r; baseColors[i*3+1] = c.g; baseColors[i*3+2] = c.b; } 
    else { baseColors[i*3] = subduedColor.r; baseColors[i*3+1] = subduedColor.g; baseColors[i*3+2] = subduedColor.b; }
    const sColor = signals[layerIdx]; signalColors[i*3] = sColor.r; signalColors[i*3+1] = sColor.g; signalColors[i*3+2] = sColor.b;
    offsetAngles[i] = angle; baseAngles[i] = angle; 
}

geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3)); geometry.setAttribute('aLayerId', new THREE.BufferAttribute(layerIds, 1)); geometry.setAttribute('aBaseColor', new THREE.BufferAttribute(baseColors, 3)); geometry.setAttribute('aSignalColor', new THREE.BufferAttribute(signalColors, 3)); geometry.setAttribute('aOffsetAngle', new THREE.BufferAttribute(offsetAngles, 1)); geometry.setAttribute('aBaseAngle', new THREE.BufferAttribute(baseAngles, 1));

const shaderMaterial = new THREE.ShaderMaterial({ vertexShader, fragmentShader, uniforms: { uTime: { value: 0 }, uTexture: { value: particleTexture }, uInteractionMode: { value: 0.0 }, uActiveLayer: { value: -1.0 }, uPixelRatio: { value: Math.min(window.devicePixelRatio, 2.0) } }, blending: THREE.AdditiveBlending, depthWrite: false, transparent: true, opacity: 1.0 });
const shaderParticles = new THREE.Points(geometry, shaderMaterial); shaderParticles.frustumCulled = false; 
breathingGroup.add(shaderParticles);


// --- SCENE 2: THE RELIEF WAVEFORM (Y = -15) ---
const waveGroup = new THREE.Group();
waveGroup.position.y = -15; // Physically separated deep below Scene 1
scene.add(waveGroup);

// The Voice Bars
const numBars = 80;
const waveBars = [];
const barGeo = new THREE.PlaneGeometry(0.015, 1);
const barMat = new THREE.MeshBasicMaterial({ color: 0xF5F5F0, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending });

for(let i = 0; i < numBars; i++) {
    const bar = new THREE.Mesh(barGeo, barMat);
    bar.position.x = (i - numBars/2) * 0.06; // Spread them out horizontally
    waveBars.push({ mesh: bar, index: i });
    waveGroup.add(bar);
}

// The Orbiting Data Nodes
const nodeCount = 150;
const nodeGeo = new THREE.BufferGeometry();
const nPos = new Float32Array(nodeCount * 3);
const nAng = new Float32Array(nodeCount);
const nRad = new Float32Array(nodeCount);

for(let i = 0; i < nodeCount; i++) {
    nAng[i] = Math.random() * Math.PI * 2;
    nRad[i] = Math.random() * 4 + 0.5;
    nPos[i*3] = Math.cos(nAng[i]) * nRad[i];
    nPos[i*3+1] = (Math.random() - 0.5) * 2.0; 
    nPos[i*3+2] = Math.sin(nAng[i]) * nRad[i];
}

nodeGeo.setAttribute('position', new THREE.BufferAttribute(nPos, 3));
nodeGeo.setAttribute('aAngle', new THREE.BufferAttribute(nAng, 1));
nodeGeo.setAttribute('aRadius', new THREE.BufferAttribute(nRad, 1));

const nodeMat = new THREE.PointsMaterial({ size: 0.05, map: particleTexture, color: 0xF5F5F0, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false });
const waveNodes = new THREE.Points(nodeGeo, nodeMat);
waveGroup.add(waveNodes);


// --- ANIMATION LOOP ---
const clock = new THREE.Clock();

function animate() {
    requestAnimationFrame(animate);
    const time = clock.getElapsedTime();

    // 1. Process Singularity (Only if we are in the upper sections to save performance)
    if(camera.position.y > -5) {
        shaderMaterial.uniforms.uTime.value = time;
        breathingGroup.rotation.z = Math.sin(time * 0.05) * 0.03;

        let mode = shaderMaterial.uniforms.uInteractionMode.value;
        let flowSpeed = 1.0 - mode; 

        if (flowSpeed > 0.001) {
            const posArray = shaderParticles.geometry.attributes.position.array;
            const angArray = shaderParticles.geometry.attributes.aOffsetAngle.array;
            for (let i = 0; i < particleCount; i++) {
                let i3 = i * 3; let x = posArray[i3]; 
                let speed = 0.004 + (0.12 / (Math.abs(x) + 0.2)); 
                angArray[i] += speed * 0.03 * flowSpeed;
                if (x > 0) { x -= (0.01 + (x * 0.003)) * flowSpeed; if (x < 0.02) x = 8; } 
                else { x += (0.01 + (Math.abs(x) * 0.003)) * flowSpeed; if (x > -0.02) x = -8; }
                const radius = Math.pow(Math.abs(x), 0.6) * 1.3 + (eventHorizonRadius + 0.02);
                posArray[i3] = x; posArray[i3+1] = Math.cos(angArray[i]) * radius; posArray[i3+2] = Math.sin(angArray[i]) * radius;
            }
            shaderParticles.geometry.attributes.position.needsUpdate = true;
            shaderParticles.geometry.attributes.aOffsetAngle.needsUpdate = true;
        }
    }

    // 2. Process The Relief Waveform (Only when we scroll down into it)
    if(camera.position.y < -5) {
        
        // A. Make the Voice Bars breathe procedurally
        waveBars.forEach(b => {
            const x = b.mesh.position.x;
            const envelope = Math.exp(- (x*x) / 1.5); // Gaussian curve makes it look like an audio clip
            const pulse = Math.sin(time * 3.0 + x * 10.0) * 0.5 + 0.5; // Ripple animation
            const breath = Math.sin(time) * 0.2 + 0.8; 
            b.mesh.scale.y = (pulse * envelope * 2.0 + 0.05) * breath;
        });

        // B. Magnetically pull the Data Nodes into the center
        const wPos = waveNodes.geometry.attributes.position.array;
        const wAng = waveNodes.geometry.attributes.aAngle.array;
        const wRad = waveNodes.geometry.attributes.aRadius.array;

        for(let i=0; i<nodeCount; i++) {
            wAng[i] += 0.01; // Orbit slowly
            wRad[i] -= 0.015; // Pull inwards
            
            // If they hit the center, respawn on the outside
            if(wRad[i] < 0.1) {
                wRad[i] = 4.0 + Math.random();
                wPos[i*3+1] = (Math.random() - 0.5) * 2.0; 
            }

            wPos[i*3] = Math.cos(wAng[i]) * wRad[i];
            wPos[i*3+2] = Math.sin(wAng[i]) * wRad[i];
        }
        waveNodes.geometry.attributes.position.needsUpdate = true;
        
        waveGroup.rotation.y = Math.sin(time * 0.2) * 0.2; // Gentle rotation of the whole wave
    }

    renderer.render(scene, camera);
}
animate();

window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
    shaderMaterial.uniforms.uPixelRatio.value = Math.min(window.devicePixelRatio, 2.0);
});


// --- GSAP SCROLL PHYSICS & TRANSITIONS ---
gsap.registerPlugin(ScrollTrigger);

gsap.utils.toArray('.scroll-fade').forEach(element => {
    gsap.fromTo(element, 
        { y: 40, opacity: 0, filter: "blur(10px)" },
        { y: 0, opacity: 1, filter: "blur(0px)", duration: 1.2, ease: "power3.out", scrollTrigger: { trigger: element, start: "top 85%", toggleActions: "play none none reverse" } }
    );
});

const scanLine = document.getElementById('scan-line-blue');
if (scanLine) { gsap.to(scanLine, { top: "70%", duration: 4, ease: "sine.inOut", yoyo: true, repeat: -1 }); }


// 3D Master Timeline
const mainTimeline = gsap.timeline({ 
    scrollTrigger: { trigger: document.body, start: "top top", end: "bottom bottom", scrub: true } 
});

mainTimeline
// 1. Hero -> Metaphor -> Architect
.to(scrollGroup.rotation, { y: Math.PI / 1.6, z: Math.PI / 3.5, ease: "none" }, 0)
.to(camera.position, { y: 2.0, ease: "none" }, 0) 

// 2. Pivot to Section 4 (The Science Matrix)
.to(scrollGroup.rotation, { y: 0, z: 0, ease: "power2.inOut" }, 0.5) 
.to(camera.position, { y: 0.2, z: 7.0, ease: "power2.inOut" }, 0.5) 
.to(shaderMaterial.uniforms.uInteractionMode, { value: 1.0, ease: "power2.inOut" }, 0.5)
.to(voidMat, { opacity: 0.3, ease: "power2.inOut" }, 0.5)
.to(rimMat.uniforms.uOpacity, { value: 0.2, ease: "power2.inOut" }, 0.5)
.to(vaporMat, { opacity: 0.2, ease: "power2.inOut" }, 0.5)
.to(innerRingMat, { opacity: 0.2, ease: "power2.inOut" }, 0.5)

// 3. The Relief Drop: Plunging the camera down to Y = -15 for the Waveform
.to(camera.position, { y: -15, z: 5.0, ease: "power2.inOut" }, 0.85);


// --- SECTION 4 HOVER INTERACTION ---
const layerMapping = { 'structure': 0.0, 'electricity': 1.0, 'energy': 2.0, 'regulation': 3.0 };
document.querySelectorAll('.science-card').forEach(card => {
    card.addEventListener('mouseenter', () => {
        const key = card.getAttribute('data-interact');
        if (layerMapping.hasOwnProperty(key)) gsap.set(shaderMaterial.uniforms.uActiveLayer, { value: layerMapping[key] });
    });
    card.addEventListener('mouseleave', () => gsap.set(shaderMaterial.uniforms.uActiveLayer, { value: -1.0 }));
});