import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useApp } from '@/context/AppContext';
import { relationsApi } from '@/api/client';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui';
import { Lightbulb, Link2, X } from '@/components/ui/icons';
import type { Relation } from '@/types';

// 关系类型颜色映射
const RELATION_COLORS: Record<string, string> = {
  family: 'rgb(var(--danger))', // 红色 - 亲属
  friend: 'rgb(var(--success))', // 绿色 - 朋友
  enemy: 'rgb(var(--state-pending))', // 灰色 - 敌人
  romance: 'rgb(var(--viz-rose))', // 粉色 - 恋人
  mentor: 'rgb(var(--viz-amber))', // 橙色 - 师徒
  colleague: 'rgb(var(--info))', // 蓝色 - 同事
  rival: 'rgb(var(--viz-violet))', // 紫色 - 对手
  ally: 'rgb(var(--viz-teal))', // 青色 - 盟友
  stranger: 'rgb(var(--text-tertiary))', // 浅灰 - 陌生人
  master: 'rgb(var(--state-failed-strong))', // 深红 - 主仆
};

/** 关系类型 → i18n key（模块顶层不能调 t()，文案在组件内取） */
const RELATION_LABEL_KEYS: Record<string, string> = {
  family: 'relation.types.family',
  friend: 'relation.types.friend',
  enemy: 'relation.types.enemy',
  romance: 'relation.types.romance',
  mentor: 'relation.types.mentor',
  colleague: 'relation.types.colleague',
  rival: 'relation.types.rival',
  ally: 'relation.types.ally',
  stranger: 'relation.types.stranger',
  master: 'relation.types.master',
};

interface GraphNode {
  id: string;
  name: string;
  role?: string;
  x: number;
  y: number;
  vx?: number;
  vy?: number;
}

interface GraphEdge {
  source: string;
  target: string;
  type: string;
  strength: number;
  label?: string;
}

interface RelationGraphTabProps {
  projectKey: string;
}

export function RelationGraphTab({ projectKey }: RelationGraphTabProps) {
  const { t } = useApp();
  const [nodes, setNodes] = useState<GraphNode[]>([]);
  const [edges, setEdges] = useState<GraphEdge[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  /** 错误态「重试」用：自增即重新触发下面的拉取 effect */
  const [reloadTick, setReloadTick] = useState(0);
  const [selectedNode, setSelectedNode] = useState<GraphNode | null>(null);
  const [hoveredEdge, setHoveredEdge] = useState<string | null>(null);
  
  const svgRef = useRef<SVGSVGElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const animationRef = useRef<number | null>(null);
  const isDragging = useRef(false);
  const draggedNode = useRef<GraphNode | null>(null);
  const dragOffset = useRef({ x: 0, y: 0 });

  // 加载关系数据
  useEffect(() => {
    if (!projectKey) return;
    
    setLoading(true);
    setError('');
    relationsApi.graph(projectKey)
      .then(data => {
        const graphData = data as { nodes: GraphNode[]; edges: GraphEdge[] };
        setNodes(graphData.nodes || []);
        setEdges(graphData.edges || []);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : t('relation.loadGraphFailed'));
      })
      .finally(() => {
        setLoading(false);
      });
  }, [projectKey, reloadTick]);

  /** 收敛阈值：整图最大单帧位移（px 之和）低于它即视为静止。 */
  const REST_EPSILON = 0.15;

  // ⚠️ simulateLayout 必须以**稳定引用**运行，否则它与下面的 rAF effect 构成正反馈。
  //   原实现是 useCallback(..., [nodes, edges]) 且每帧 setNodes(新数组)：
  //     setNodes → nodes 变 → simulateLayout 引用变 → 依赖它的 effect 重跑
  //     → 同时 animate() 自己又无条件续下一帧
  //   于是每秒 60 次 setNodes + 整图重渲染 + 一次 O(n²) 斥力计算，**永不停止**，
  //   而画面早已收敛、一动不动（纯烧 CPU）。把 nodes 收进 ref 后 deps 为空，引用恒定。
  const nodesRef = useRef<GraphNode[]>(nodes);
  useEffect(() => { nodesRef.current = nodes; }, [nodes]);

  /**
   * 跑一步力导向布局。
   * @returns 是否还在运动。false = 已收敛，调用方应停帧。
   */
  const simulateLayout = useCallback((): boolean => {
    const current = nodesRef.current;
    if (current.length === 0) return false;

    const alpha = 0.3;
    const centerX = 400;
    const centerY = 300;

    // 初始化位置（如果没有）
    let updatedNodes = current.map(n => ({
      ...n,
      x: n.x ?? centerX + (Math.random() - 0.5) * 200,
      y: n.y ?? centerY + (Math.random() - 0.5) * 200,
      vx: n.vx ?? 0,
      vy: n.vy ?? 0,
    }));

    // 节点间斥力
    for (let i = 0; i < updatedNodes.length; i++) {
      for (let j = i + 1; j < updatedNodes.length; j++) {
        const a = updatedNodes[i];
        const b = updatedNodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (alpha * 500) / dist;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        
        if (!isDragging.current || draggedNode?.current?.id !== a.id) {
          a.vx -= fx;
          a.vy -= fy;
        }
        if (!isDragging.current || draggedNode?.current?.id !== b.id) {
          b.vx += fx;
          b.vy += fy;
        }
      }
    }

    // 边的引力
    for (const edge of edges) {
      const source = updatedNodes.find(n => n.id === edge.source);
      const target = updatedNodes.find(n => n.id === edge.target);
      if (!source || !target) continue;

      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (alpha * dist) / 10;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;

      if (!isDragging.current || draggedNode?.current?.id !== source.id) {
        source.vx += fx;
        source.vy += fy;
      }
      if (!isDragging.current || draggedNode?.current?.id !== target.id) {
        target.vx -= fx;
        target.vy -= fy;
      }
    }

    // 中心引力
    for (const node of updatedNodes) {
      if (isDragging.current && draggedNode?.current?.id === node.id) continue;
      
      const dx = centerX - node.x;
      const dy = centerY - node.y;
      node.vx += dx * 0.001;
      node.vy += dy * 0.001;
    }

    // 应用速度并限制边界
    updatedNodes = updatedNodes.map(n => {
      if (isDragging.current && draggedNode?.current?.id === n.id) return n;
      
      const speed = 0.8;
      const vx = Math.max(-speed, Math.min(speed, n.vx * 0.9));
      const vy = Math.max(-speed, Math.min(speed, n.vy * 0.9));
      return {
        ...n,
        x: Math.max(50, Math.min(750, n.x + vx)),
        y: Math.max(50, Math.min(550, n.y + vy)),
        vx,
        vy,
      };
    });

    // 收敛判定：单帧位移已低于阈值时**不调 setNodes**。
    // 这是省 CPU 的关键 —— 只要还调 setNodes，React 就必然重渲染整张关系图，
    // 哪怕画面上一个像素都没动。
    let maxDelta = 0;
    for (let i = 0; i < updatedNodes.length; i++) {
      const d = Math.abs(updatedNodes[i].x - current[i].x)
              + Math.abs(updatedNodes[i].y - current[i].y);
      if (d > maxDelta) maxDelta = d;
    }
    if (maxDelta < REST_EPSILON) return false;

    setNodes(updatedNodes);
    return true;
    // ⚠️ deps 刻意为空：本函数读的是 nodesRef.current。写 [nodes, edges] 会让引用
    //    每帧变化 → 下面的 effect 每帧重跑 → 回到「60fps 重渲染」的老问题。
  }, []);

  // 拖拽结束后要重新唤醒布局（拖拽会直接 setNodes 移动节点，但 edges 没变，
  // 靠 edges 依赖重启不了）。用一个自增计数器做显式的「重新布局」信号。
  const [layoutEpoch, setLayoutEpoch] = useState(0);

  // 动画循环：**只在还在运动时续帧**。
  //  - simulateLayout 返回 false（已收敛）即停 —— 静止后 CPU 占用归零；
  //  - 标签页不可见（document.hidden）即停 —— 后台标签每帧做 O(n²) 纯属烧电；
  //  - 三个来源各自会重启它：edges 变化（数据重载）、layoutEpoch 自增（拖拽结束）、
  //    以及首次挂载。
  useEffect(() => {
    let running = true;
    const animate = () => {
      if (!running) return;
      if (document.hidden) { animationRef.current = 0; return; }
      animationRef.current = simulateLayout() ? requestAnimationFrame(animate) : 0;
    };
    animationRef.current = requestAnimationFrame(animate);
    return () => {
      running = false;
      if (animationRef.current) {
        cancelAnimationFrame(animationRef.current);
        animationRef.current = 0;
      }
    };
  }, [simulateLayout, edges, layoutEpoch]);

  // SVG 事件处理
  const handleMouseDown = (e: React.MouseEvent, node: GraphNode) => {
    e.preventDefault();
    isDragging.current = true;
    draggedNode.current = node;
    
    const svg = svgRef.current;
    if (svg) {
      const rect = svg.getBoundingClientRect();
      dragOffset.current = {
        x: e.clientX - rect.left - node.x,
        y: e.clientY - rect.top - node.y,
      };
    }
  };

  const handleMouseMove = (e: React.MouseEvent) => {
    if (!isDragging.current || !draggedNode.current || !svgRef.current) return;
    
    const rect = svgRef.current.getBoundingClientRect();
    const x = e.clientX - rect.left - dragOffset.current.x;
    const y = e.clientY - rect.top - dragOffset.current.y;
    
    draggedNode.current.x = Math.max(50, Math.min(750, x));
    draggedNode.current.y = Math.max(50, Math.min(550, y));
    draggedNode.current.vx = 0;
    draggedNode.current.vy = 0;
    
    setNodes([...nodes]);
  };

  const handleMouseUp = () => {
    isDragging.current = false;
    draggedNode.current = null;
    // 拖拽直接改了节点坐标但 edges 没变，靠 [edges] 重启不了布局 ——
    // 显式自增一次让力导向重新跑起来，把被拖开的节点重新松弛回平衡态。
    setLayoutEpoch((e) => e + 1);
  };

  // 加载态：沿用「标题行 + 画布高度 + 底部统计」的形态，避免画布区空白跳变
  if (loading) {
    return (
      <div className="space-y-4 min-w-0" role="status" aria-live="polite" aria-label={t('common.loading')}>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-4 w-48" />
        </div>
        <Skeleton className="h-[360px] rounded-lg sm:h-[500px]" />
        <Skeleton className="h-4 w-56" />
      </div>
    );
  }

  // 硬失败：整块关系图没有任何数据可展示 → 错误态 + 重试
  if (error) {
    return (
      <ErrorState
        title={t('relation.loadFailed')}
        description={error}
        onRetry={() => setReloadTick((x) => x + 1)}
      />
    );
  }

  if (nodes.length === 0) {
    return (
      <EmptyState
        icon={<Link2 className="h-10 w-10" />}
        title={t('relation.noRelations')}
        description={t('relation.noChars')}
      />
    );
  }

  return (
    <div className="space-y-4 min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-lg font-semibold text-ink-1">{t('relation.title')}</h3>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          {Object.entries(RELATION_LABEL_KEYS).slice(0, 5).map(([type, labelKey]) => (
            <span key={type} className="flex items-center gap-1">
              <span className="w-3 h-3 rounded-full" style={{ backgroundColor: RELATION_COLORS[type] }}></span>
              {t(labelKey)}
            </span>
          ))}
        </div>
      </div>

      <div
        ref={containerRef}
        className="relative bg-surface rounded-lg border border-line overflow-auto h-[360px] sm:h-[500px]"
      >
        <svg
          ref={svgRef}
          width="100%"
          height="100%"
          viewBox="0 0 800 600"
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
          className="cursor-grab active:cursor-grabbing"
        >
          {/* 背景网格 */}
          <defs>
            <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
              <path d="M 40 0 L 0 0 0 40" fill="none" stroke="currentColor" strokeWidth="0.5" className="text-line" />
            </pattern>
          </defs>
          <rect width="100%" height="100%" fill="url(#grid)" />

          {/* 边 */}
          {edges.map((edge, idx) => {
            const source = nodes.find(n => n.id === edge.source);
            const target = nodes.find(n => n.id === edge.target);
            if (!source || !target) return null;

            const color = RELATION_COLORS[edge.type] || 'rgb(var(--text-tertiary))';
            const width = Math.abs(edge.strength) * 3 + 1;
            const isHovered = hoveredEdge === `${edge.source}-${edge.target}`;

            return (
              <g key={idx}>
                <line
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  stroke={color}
                  strokeWidth={isHovered ? width + 2 : width}
                  opacity={isHovered ? 1 : 0.6}
                  onMouseEnter={() => setHoveredEdge(`${edge.source}-${edge.target}`)}
                  onMouseLeave={() => setHoveredEdge(null)}
                  className="cursor-pointer"
                />
                {/* 边的中点标签 */}
                {isHovered && (
                  <g>
                    <rect
                      x={(source.x + target.x) / 2 - 30}
                      y={(source.y + target.y) / 2 - 12}
                      width="60"
                      height="20"
                      fill="rgb(var(--text-primary) / 0.7)"
                      rx="4"
                    />
                    <text
                      x={(source.x + target.x) / 2}
                      y={(source.y + target.y) / 2}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      fill="white"
                      fontSize="10"
                    >
                      {t(RELATION_LABEL_KEYS[edge.type] || edge.type)}
                    </text>
                  </g>
                )}
              </g>
            );
          })}

          {/* 节点 */}
          {nodes.map((node) => {
            const isSelected = selectedNode?.id === node.id;
            return (
              <g
                key={node.id}
                transform={`translate(${node.x}, ${node.y})`}
                onMouseDown={(e) => handleMouseDown(e, node)}
                onClick={() => setSelectedNode(node)}
                className="cursor-pointer"
              >
                {/* 节点光环（选中时） */}
                {isSelected && (
                  <circle
                    r={35}
                    fill="none"
                    stroke="rgb(var(--brand))"
                    strokeWidth="3"
                    opacity="0.5"
                  />
                )}
                
                {/* 节点圆形 */}
                <circle
                  r={25}
                  fill={isSelected ? 'rgb(var(--brand))' : 'rgb(var(--bg-surface))'}
                  stroke={isSelected ? 'rgb(var(--brand-hover))' : 'rgb(var(--border-strong))'}
                  strokeWidth="2"
                  className="transition-colors duration-200"
                />
                
                {/* 角色首字母 */}
                <text
                  textAnchor="middle"
                  dominantBaseline="middle"
                  fill={isSelected ? 'rgb(var(--bg-surface))' : 'rgb(var(--text-primary))'}
                  fontSize="14"
                  fontWeight="bold"
                >
                  {node.name?.[0] || '?'}
                </text>
                
                {/* 角色名称 */}
                <text
                  y={40}
                  textAnchor="middle"
                  fill="rgb(var(--text-primary))"
                  fontSize="11"
                  className=""
                >
                  {node.name}
                </text>
                
                {/* 角色类型 */}
                {node.role && (
                  <text
                    y={52}
                    textAnchor="middle"
                    fill="rgb(var(--text-tertiary))"
                    fontSize="9"
                  >
                    {node.role}
                  </text>
                )}
              </g>
            );
          })}
        </svg>

        {/* 节点详情面板 */}
        {selectedNode && (
          <div className="absolute top-4 right-4 w-64 bg-surface rounded-lg shadow-lg border border-line p-4">
            <div className="flex items-start justify-between mb-3">
              <div>
                <h4 className="font-semibold text-ink-1">{selectedNode.name}</h4>
                {selectedNode.role && (
                  <p className="text-xs text-ink-2">{selectedNode.role}</p>
                )}
              </div>
              <button
                onClick={() => setSelectedNode(null)}
                aria-label={t('relation.closeDetails')}
                className="text-ink-3 hover:text-ink-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            
            <div className="space-y-2">
              <p className="text-xs text-ink-2">
                {t('relation.relationCount', { n: edges.filter(e => e.source === selectedNode.id || e.target === selectedNode.id).length })}
              </p>
              
              {/* 关联关系列表 */}
              <div className="space-y-1">
                {edges
                  .filter(e => e.source === selectedNode.id || e.target === selectedNode.id)
                  .slice(0, 5)
                  .map((edge, idx) => {
                    const otherId = edge.source === selectedNode.id ? edge.target : edge.source;
                    const otherNode = nodes.find(n => n.id === otherId);
                    if (!otherNode) return null;
                    
                    return (
                      <div
                        key={idx}
                        className="flex items-center gap-2 text-xs"
                      >
                        <span
                          className="w-2 h-2 rounded-full shrink-0"
                          style={{ backgroundColor: RELATION_COLORS[edge.type] || 'rgb(var(--text-tertiary))' }}
                        ></span>
                        <span className="text-ink-2">{otherNode.name}</span>
                        <span className="text-ink-3 ml-auto">
                          {t(RELATION_LABEL_KEYS[edge.type] || edge.type)}
                        </span>
                      </div>
                    );
                  })}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 操作提示 */}
      <div className="flex flex-wrap items-center justify-between gap-1 text-xs text-ink-2">
        <span className="inline-flex items-center gap-1"><Lightbulb className="h-3.5 w-3.5" /> {t('relation.dragHint')}</span>
        <span>{t('relation.stats', { nodes: nodes.length, edges: edges.length })}</span>
      </div>
    </div>
  );
}
