(function(){
  // perf-quick-wins: viewport culling, simple simplification, arrow throttle, deferred search index
  function simplifyCoordinateList(coords,maxPoints=600){
    if(!Array.isArray(coords) || coords.length <= maxPoints) return coords;
    const step=Math.ceil(coords.length/maxPoints);
    const out=[];
    for(let i=0;i<coords.length;i+=step) out.push(coords[i]);
    if(out.length && out[out.length-1] !== coords[coords.length-1]) out.push(coords[coords.length-1]);
    return out;
  }

  function simplifyFeatureGeometry(feature,maxPoints=600){
    if(!feature || !feature.geometry) return feature;
    const geometry=feature.geometry;
    if(geometry.type==="LineString" && Array.isArray(geometry.coordinates)){
      geometry.coordinates=simplifyCoordinateList(geometry.coordinates,maxPoints);
    }else if(geometry.type==="MultiLineString" && Array.isArray(geometry.coordinates)){
      geometry.coordinates=geometry.coordinates.map(part=>simplifyCoordinateList(part,maxPoints));
    }
    return feature;
  }

  function featureLatLngBounds(feature){
    if(!feature || !feature.geometry) return null;
    const geometry=feature.geometry;
    const coords = geometry.type==="MultiLineString"
      ? geometry.coordinates.flat()
      : geometry.coordinates;
    if(!Array.isArray(coords) || !coords.length) return null;
    let minLat=90,minLng=180,maxLat=-90,maxLng=-180;
    for(const pair of coords){
      if(!Array.isArray(pair) || pair.length<2) continue;
      const lat=Number(pair[1]), lng=Number(pair[0]);
      if(!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
      if(lat<minLat) minLat=lat;
      if(lat>maxLat) maxLat=lat;
      if(lng<minLng) minLng=lng;
      if(lng>maxLng) maxLng=lng;
    }
    if(!Number.isFinite(minLat) || !Number.isFinite(maxLat) || !Number.isFinite(minLng) || !Number.isFinite(maxLng)) return null;
    return [[minLat,minLng],[maxLat,maxLng]];
  }

  function featureVisibleInMap(feature){
    try{
      if(!feature || !feature.geometry) return true;
      const bounds=featureLatLngBounds(feature);
      if(!bounds) return true;
      const mapBounds=map.getBounds().pad(0.35);
      return !(
        bounds[1][0] < mapBounds.getSouth() ||
        bounds[0][0] > mapBounds.getNorth() ||
        bounds[1][1] < mapBounds.getWest() ||
        bounds[0][1] > mapBounds.getEast()
      );
    }catch(e){
      return true;
    }
  }

  function rebuildSearchIndex(){
    if(!trailLayer) return;
    searchIndex=[];
    trailLayer.eachLayer(layer=>{
      const feature=layer.feature;
      if(!feature) return;
      const p=feature.properties||{};
      const name=p.name||p["mtb:name"];
      if(!name) return;
      if(!passesFilter(p)) return;
      searchIndex.push({name:String(name),layer,feature});
    });
    searchIndex=[...new Map(searchIndex.map(item=>[item.name+"|"+featureId(item.feature),item])).values()];
  }

  // Wait for original variables to be available then patch
  function safePatch(){
    if(typeof trailRenderer==='undefined' || !trailRenderer || typeof trailRenderer._updatePoly!=='function' || typeof L==='undefined' || typeof map==='undefined'){
      setTimeout(safePatch,120);
      return;
    }

    // Patch arrow drawing: only when zoom >= 15
    try{
      const updateTrailPoly=trailRenderer._updatePoly;
      trailRenderer._updatePoly=function(layer,closed){
        updateTrailPoly.call(this,layer,closed);
        try{
          if(!this._drawing || closed || !settings.showTrailDirection || map.getZoom() < 15 || !passesFilter(layer.feature?.properties||{})) return;
          const context=this._ctx;
          if(!context || !layer._parts) return;
          const properties=layer.feature?.properties||{};
          const reverse=["-1","reverse"].includes(String(properties.oneway||properties["oneway:bicycle"]||"").toLowerCase());
          const spacing=56,size=7;
          context.save();
          context.strokeStyle="rgba(255,255,255,.95)";
          context.lineWidth=settings.highContrast?2.4:2;
          context.lineCap="round";
          context.lineJoin="round";
          for(const rawPart of layer._parts){
            const part=reverse ? [...rawPart].reverse() : rawPart;
            let remaining=spacing/2;
            for(let index=1;index<part.length;index++){
              const start=part[index-1],end=part[index];
              const dx=end.x-start.x,dy=end.y-start.y,length=Math.hypot(dx,dy);
              if(length<1) continue;
              const ux=dx/length,uy=dy/length;
              while(remaining<=length){
                const x=start.x+ux*remaining,y=start.y+uy*remaining;
                const backX=x-ux*size,backY=y-uy*size;
                context.beginPath();
                context.moveTo(backX-uy*size*.65,backY+ux*size*.65);
                context.lineTo(x,y);
                context.lineTo(backX+uy*size*.65,backY-ux*size*.65);
                context.stroke();
                remaining+=spacing;
              }
              remaining-=length;
            }
          }
          context.restore();
        }catch(e){
          // swallow
        }
      };
    }catch(e){
      // ignore
    }

    // Override rebuildLayers and loadData to include culling + simplification
    try{
      const origRebuild = window.rebuildLayers;
      window.rebuildLayers = function(fit=false){
        if(trailLayer) map.removeLayer(trailLayer);
        if(outlineLayer) map.removeLayer(outlineLayer);

        const layerFilter = f => isLineFeature(f) && featureVisibleInMap(f);

        outlineLayer = L.geoJSON(geoData,{
          filter:layerFilter,
          renderer:outlineRenderer,
          pane:"trailOutlinePane",
          style:styleOutline,
          interactive:false
        }).addTo(map);

        trailLayer = L.geoJSON(geoData,{
          filter:layerFilter,
          renderer:trailRenderer,
          pane:"trailPane",
          style:styleMain,
          onEachFeature:(f,l)=>{
            l.on("click",()=>{ if(passesFilter(f.properties||{})) selectFeature(l,f); });
          }
        }).addTo(map);

        rebuildSearchIndex();
        if(fit && trailLayer.getBounds().isValid()) map.fitBounds(trailLayer.getBounds(),{padding:[20,20]});
        renderLegend();
      };
    }catch(e){ }

    try{
      const origLoadData = window.loadData;
      window.loadData = async function(cacheBust=false){
        if(cacheBust) elevationProfilesCache.clear();
        const suffix=cacheBust ? `?t=${Date.now()}` : "";
        const collections = await Promise.all(TRAIL_FILES.map(async file=>{
          const r = await fetch(file+suffix,{cache:cacheBust?"reload":"default"});
          if(!r.ok) throw new Error(`${file} konnte nicht geladen werden`);
          const document = await r.json();
          const chunk = file.slice(7,9);
          for(const feature of document.features||[]){ feature._trailFile=chunk; simplifyFeatureGeometry(feature,600); }
          return document;
        }));
        geoData={ type:"FeatureCollection", features:collections.flatMap(c=>c.features||[]) };
        rebuildLayers(!map._loaded);
        const el=document.getElementById("dataStatus");
        if(el) el.textContent = `${geoData.features?.length||0} OSM-Objekte geladen · Canvas-Darstellung aktiv`;
      };
    }catch(e){ }

    // rebuild on moveend w/ debounce
    try{
      let rebuildTimer=null;
      map.on("moveend zoomend rotateend",()=>{
        clearTimeout(rebuildTimer);
        rebuildTimer=setTimeout(()=>{ if(geoData) rebuildLayers(false); }, 250);
      });
    }catch(e){}
  }

  safePatch();
})();
