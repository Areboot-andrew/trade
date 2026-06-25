import { useState, useEffect, useRef } from 'react'
import './index.css'

interface Order {
  price: number;
  amount: number;
  status: string;
  reason?: string;
  margin?: number;
  notional_leveraged?: number;
}

interface ManagedOrder {
  manager_id: string;
  side: string;
  price: number;
  quantity: number;
  margin_usd: number;
  source: string;
  status: string;
}

// Helper to get decimal places from stepSize (e.g. "0.001" -> 3)
const getQuantityDecimals = (symbol: string, precisions: Record<string, any>) => {
  const data = precisions[symbol];
  if (!data || !data.quantity_precision_str) return 5;
  const step = data.quantity_precision_str;
  if (!step.includes('.')) return 0;
  return step.split('.')[1].length;
};

const getPriceDecimals = (symbol: string, precisions: Record<string, any>) => {
  const data = precisions[symbol];
  if (!data || !data.price_precision_str) return 2;
  const tick = data.price_precision_str;
  if (!tick.includes('.')) return 0;
  return tick.split('.')[1].length;
};

interface LiveOrder {
  orderId: string;
  symbol: string;
  side: string;
  type: string;
  price: string;
  origQty: string;
  status: string;
  positionSide: string;
}

interface LivePosition {
  symbol: string;
  amount: number;
  entry_price: number;
  mark_price: number;
  pnl_usd: number;
  leverage: string;
  position_side: string;
  projected_avg_price?: number;
}

interface TradeHistoryItem {
  time: number;
  symbol: string;
  side: string;
  price: string;
  qty: string;
  realizedPnl: string;
  commission: string;
  commissionAsset: string;
}

interface LogEntry {
  time: string;
  message: string;
}

// Таблиці винесено назовні щоб уникнути перестворення DOM-вузлів (що і викликає скидання скролу)
const OrderTable = ({ title, orders, type, symbol, assetPrecisions }: { 
  title: string, 
  orders: Order[], 
  type: 'long' | 'short', 
  symbol: string,
  assetPrecisions: Record<string, any>
}) => (
  <div className="panel" style={{ height: '300px', overflowY: 'auto' }}>
    <div className="panel-header">
      <span className={type === 'long' ? 'long-title' : 'short-title'}>{title}</span>
      <span className="count-badge">{orders.length} grid orders</span>
    </div>
    {orders.length > 0 ? (
      <table className="data-table">
        <thead>
          <tr>
            <th>Price</th>
            <th>Amount ({symbol.replace('USDT', '')})</th>
            <th>Margin ($)</th>
            <th>Lev Size</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order, i) => (
            <tr key={`${order.price}-${order.amount}-${i}`}>
              <td>{order.price ? order.price.toFixed(getPriceDecimals(symbol, assetPrecisions)) : '-'}</td>
              <td>{order.amount?.toFixed(getQuantityDecimals(symbol, assetPrecisions)) || '-'}</td>
              <td>{order.margin?.toFixed(2) || '-'}</td>
              <td>{order.notional_leveraged?.toFixed(2) || '-'}</td>
              <td>
                <span style={{ color: 'var(--text-muted)' }}>
                  {order.reason || 'manual'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    ) : (
      <div className="empty-state">Calculating {type} grid...</div>
    )}
  </div>
)

// @ts-ignore
const _LiveOrdersTable = ({ openOrders, assetPrecisions }: { openOrders: LiveOrder[], assetPrecisions: Record<string, any> }) => (
  <div className="panel" style={{ height: '300px', overflowY: 'auto' }}>
    <div className="panel-header">
      <span style={{color: 'white', fontWeight: 600}}>Live Open Orders (Binance)</span>
      <span className="count-badge">{openOrders.length} active</span>
    </div>
    {openOrders.length > 0 ? (
      <table className="data-table">
        <thead>
          <tr>
            <th>Side/Type</th>
            <th>Pos Side</th>
            <th>Price</th>
            <th>Amount</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {openOrders.map((o) => (
            <tr key={o.orderId}>
              <td><span style={{color: o.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}}>{o.side}</span> {o.type}</td>
              <td>{o.positionSide}</td>
              <td>{parseFloat(o.price).toFixed(getPriceDecimals(o.symbol, assetPrecisions))}</td>
              <td>{o.origQty}</td>
              <td>{o.status}</td>
            </tr>
          ))}
        </tbody>
      </table>
    ) : (
      <div className="empty-state">No Live Orders Open on Binance</div>
    )}
  </div>
)

const LivePositionsTable = ({ positions, userLeverage, assetPrecisions }: { 
  positions: LivePosition[], 
  userLeverage: number,
  assetPrecisions: Record<string, any>
}) => (
  <div className="panel" style={{ height: '300px', overflowY: 'auto' }}>
    <div className="panel-header">
      <span style={{color: 'white', fontWeight: 600}}>Live Positions (Binance)</span>
      <span className="count-badge">{positions.length} active | Your Leverage: {userLeverage}x</span>
    </div>
    {positions.length > 0 ? (
      <table className="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Pos Side</th>
            <th>Amount</th>
            <th>Entry Price</th>
            <th>Mark Price</th>
            <th>Margin ($)</th>
            <th>PNL ($)</th>
            <th>PNL (%)</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const margin = Math.abs(p.amount) * parseFloat(p.entry_price.toString()) / userLeverage;
            const pct = margin > 0 ? (parseFloat(p.pnl_usd.toString()) / margin) * 100 : 0;
            return (
            <tr key={`${p.symbol}-${p.position_side}`}>
              <td>{p.symbol}</td>
              <td>{p.position_side}</td>
              <td style={{color: p.amount > 0 ? 'var(--accent-green)' : (p.amount < 0 ? 'var(--accent-red)' : 'white')}}>
                {parseFloat(p.amount.toString()).toFixed(getQuantityDecimals(p.symbol, assetPrecisions))}
              </td>
              <td>{parseFloat(p.entry_price.toString()).toFixed(getPriceDecimals(p.symbol, assetPrecisions))}</td>
              <td>{parseFloat(p.mark_price.toString()).toFixed(getPriceDecimals(p.symbol, assetPrecisions))}</td>
              <td>{margin.toFixed(2)}</td>
              <td style={{color: p.pnl_usd >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}}>{parseFloat(p.pnl_usd.toString()).toFixed(2)}</td>
              <td style={{color: pct >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600}}>
                {(pct >= 0 ? '+' : '') + pct.toFixed(2) + '%'}
              </td>
            </tr>
            );
          })}
        </tbody>
      </table>
    ) : (
      <div className="empty-state">No Open Positions</div>
    )}
  </div>
)

const TradeHistoryTable = ({ tradeHistory, assetPrecisions }: { 
  tradeHistory: TradeHistoryItem[],
  assetPrecisions: Record<string, any>
}) => (
  <div className="panel" style={{ height: '300px', overflowY: 'auto' }}>
    <div className="panel-header">
      <span style={{color: 'white', fontWeight: 600}}>Trade History</span>
      <span className="count-badge">{tradeHistory.length} trades</span>
    </div>
    {tradeHistory.length > 0 ? (
      <table className="data-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Symbol</th>
            <th>Side</th>
            <th>Price</th>
            <th>Qty</th>
            <th>Realized PNL</th>
            <th>Commission</th>
          </tr>
        </thead>
        <tbody>
          {tradeHistory.map((t, i) => (
            <tr key={`${t.time}-${t.symbol}-${t.side}-${i}`}>
              <td>{new Date((t as any).time).toLocaleTimeString()}</td>
              <td>{t.symbol}</td>
              <td style={{color: t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}}>{t.side}</td>
              <td>{parseFloat(t.price).toFixed(getPriceDecimals(t.symbol, assetPrecisions))}</td>
              <td>{parseFloat(t.qty.toString()).toFixed(getQuantityDecimals(t.symbol, assetPrecisions))}</td>
              <td style={{color: parseFloat(t.realizedPnl) > 0 ? 'var(--accent-green)' : (parseFloat(t.realizedPnl) < 0 ? 'var(--accent-red)' : 'white')}}>{parseFloat(t.realizedPnl).toFixed(4)}</td>
              <td>{t.commission}</td>
            </tr>
          ))}
        </tbody>
      </table>
    ) : (
      <div className="empty-state">No Trade History</div>
    )}
  </div>
)

const ManagedOrdersTable = ({ orders, symbol, assetPrecisions }: { 
  orders: ManagedOrder[], 
  symbol: string,
  assetPrecisions: Record<string, any>
}) => (
  <div className="panel" style={{ height: '350px', overflowY: 'auto' }}>
    <div className="panel-header">
      <span style={{color: 'var(--accent-blue)', fontWeight: 600}}>Live Managed Orders (Bot Activity)</span>
      <span className="count-badge">{orders.length} active items</span>
    </div>
    {orders.length > 0 ? (
      <table className="data-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Side</th>
            <th>Target Price</th>
            <th>Qty</th>
            <th>Margin</th>
            <th>Source</th>
            <th>Bot Status</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((o, i) => {
            const statusLower = o.status.toLowerCase();
            let statusColor = 'var(--text-muted)';
            let statusBg = 'rgba(255, 255, 255, 0.05)';
            
            if (statusLower.includes('безпека') || statusLower.includes('repositioned')) {
              statusColor = '#fbbf24'; // Amber/Orange for Safety
              statusBg = 'rgba(251, 191, 36, 0.15)';
            } else if (statusLower.includes('моніторинг') || statusLower.includes('monitoring')) {
              statusColor = '#60a5fa'; // Blue
              statusBg = 'rgba(96, 165, 250, 0.1)';
            } else if (statusLower.includes('виставлено') || statusLower.includes('placed')) {
              statusColor = '#34d399'; // Green/Cyan
              statusBg = 'rgba(52, 211, 153, 0.15)';
            } else if (statusLower.includes('помилка') || statusLower.includes('error') || statusLower.includes('fail')) {
              statusColor = '#f87171'; // Red
              statusBg = 'rgba(248, 113, 113, 0.2)';
            } else if (statusLower.includes('тригер') || statusLower.includes('trigger')) {
              statusColor = '#fb7185'; // Rose
              statusBg = 'rgba(251, 113, 133, 0.15)';
            }

            return (
              <tr key={`${o.manager_id}-${i}`} style={{ background: statusLower.includes('безпека') ? 'rgba(251, 191, 36, 0.03)' : 'transparent' }}>
                <td>{o.manager_id.toString().slice(-4)}</td>
                <td style={{color: o.side.includes('BUY') ? 'var(--accent-green)' : (o.side.includes('SELL') ? 'var(--accent-red)' : 'white')}}>
                  {o.side}
                </td>
                <td>{o.price > 0 ? o.price.toFixed(getPriceDecimals(symbol, assetPrecisions)) : '-'}</td>
                <td>{o.quantity > 0 ? o.quantity.toFixed(getQuantityDecimals(symbol, assetPrecisions)) : '-'}</td>
                <td>{o.margin_usd > 0 ? '$' + o.margin_usd.toFixed(2) : '-'}</td>
                <td style={{fontSize: '0.75rem', color: statusLower.includes('безпека') ? '#fbbf24' : 'var(--text-muted)'}}>{o.source}</td>
                <td>
                  <span style={{
                    padding: '0.2rem 0.6rem', 
                    borderRadius: '4px', 
                    background: statusBg, 
                    color: statusColor,
                    fontSize: '0.75rem',
                    fontWeight: 600,
                    border: `1px solid ${statusBg}`
                  }}>
                    {o.status}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    ) : (
      <div className="empty-state">No Live Bot Activity</div>
    )}
  </div>
)

function App() {
  const [connected, setConnected] = useState(false)
  const [marketPrice, setMarketPrice] = useState<number>(0)
  const [symbol, setSymbol] = useState<string>("BTCUSDT")
  const [availableSymbols, setAvailableSymbols] = useState<string[]>(["BTCUSDT"])
  const [longGrid, setLongGrid] = useState<Order[]>([])
  const [shortGrid, setShortGrid] = useState<Order[]>([])
  const [openOrders, setOpenOrders] = useState<LiveOrder[]>([])
  const [positions, setPositions] = useState<LivePosition[]>([])
  const [assetPrecisions, setAssetPrecisions] = useState<Record<string, any>>({})
  const [tradeHistory, setTradeHistory] = useState<TradeHistoryItem[]>([])
  const [managedOrders, setManagedOrders] = useState<ManagedOrder[]>([])
  const [logs, setLogs] = useState<LogEntry[]>([])
  const wsRef = useRef<WebSocket | null>(null)

  const [leverage, setLeverage] = useState("20")
  const [initialMargin, setInitialMargin] = useState("20")
  const [marginMultiplier, setMarginMultiplier] = useState("1.2")
  const [clusterCount, setClusterCount] = useState("3")

  const [liveTradingStatus, setLiveTradingStatus] = useState("Stopped")
  const [binanceStatus, setBinanceStatus] = useState("Disconnected")
  const [inputApiKey, setInputApiKey] = useState("")
  const [inputApiSecret, setInputApiSecret] = useState("")
  const [botPositionType, _setBotPositionType] = useState("Long")
  const [_botGrid, _setBotGrid] = useState<Order[]>([])
  const [capitalLimit, setCapitalLimit] = useState("300")
  const [minStake, setMinStake] = useState("1.1")
  const [recalcInterval, setRecalcInterval] = useState("8")

  const [longPnlUsd, setLongPnlUsd] = useState(0)
  const [shortPnlUsd, setShortPnlUsd] = useState(0)
  const [longPnlPercent, setLongPnlPercent] = useState(0)
  const [shortPnlPercent, setShortPnlPercent] = useState(0)
  const [readyTfs, setReadyTfs] = useState<string[]>([])
  const [strategyProfile, setStrategyProfile] = useState("moderate_v2")

  // --- Manual Order State ---
  const [manualSide, setManualSide] = useState("BUY")
  const [manualType, setManualType] = useState("LIMIT")
  const [manualQty, setManualQty] = useState("")
  const [manualPrice, setManualPrice] = useState("")
  const [manualPosSide, setManualPosSide] = useState("LONG")

  // --- Cancel Order State ---
  const [selectedOrderId, setSelectedOrderId] = useState("")

  // --- TP Levels State (up to 5) ---
  const [tpLevels, setTpLevels] = useState<{pnl: string, close: string}[]>([
    {pnl: "", close: ""},
    {pnl: "", close: ""}
  ])

  const addLog = (message: string) => {
    const time = new Date().toLocaleTimeString();
    setLogs(prev => [{time, message}, ...prev].slice(0, 50));
  }

  const sendConfigToServer = () => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: "update_config",
        symbol: symbol, 
        leverage: leverage,
        initial_margin: initialMargin,
        margin_multiplier: marginMultiplier,
        cluster_count: clusterCount,
        capital_limit: capitalLimit,
        min_stake: minStake,
        recalc_interval_hours: recalcInterval
      }))
      addLog(`Config updated: Sym=${symbol}, Lev=${leverage}x, InitialMargin=$${initialMargin}, CapitalLimit=$${capitalLimit}, MinStake=$${minStake}, Recalc=${recalcInterval}h`);
    }
  }

  const handleApiRequest = async (endpoint: string) => {
    try {
      const response = await fetch(`http://localhost:8000/api/${endpoint}`, { method: 'POST' })
      const data = await response.json()
      addLog(`API Response (${endpoint}): ${JSON.stringify(data)}`)
      if (endpoint === 'connect') setBinanceStatus("Connecting...")
      if (endpoint === 'disconnect') setBinanceStatus("Disconnected")
      if (endpoint === 'start_trading') setLiveTradingStatus("Running")
      if (endpoint === 'stop_trading') setLiveTradingStatus("Stopped")
      if (endpoint === 'shutdown') {
          addLog("⚠️ Server shutting down...");
          setConnected(false);
          setBinanceStatus("Disconnected");
      }
    } catch (err) {
      addLog(`API Request Failed (${endpoint}): ${err}`)
    }
  }

  const handleSetProfile = async (profileName: string) => {
    try {
      setStrategyProfile(profileName);
      const response = await fetch(`http://localhost:8000/api/set_profile`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_name: profileName })
      })
      await response.json()
      addLog(`Profile updated: ${profileName}`)
      // Trigger grid recalculation if needed
      handleJsonApiRequest('calculate_grid', { position_type: botPositionType })
    } catch (err) {
      addLog(`Failed to update profile: ${err}`)
    }
  }

  const handleJsonApiRequest = async (endpoint: string, body: any) => {
    try {
      const response = await fetch(`http://localhost:8000/api/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
      const data = await response.json()
      addLog(`API Response (${endpoint}): ${JSON.stringify(data)}`)
      
      if (endpoint === 'calculate_grid' && data.status === 'success') {
          if (data.long_grid) setLongGrid(data.long_grid);
          if (data.short_grid) setShortGrid(data.short_grid);
      }
      if (endpoint === 'clear_grids' && data.status === 'cleared') {
          setLongGrid([]);
          setShortGrid([]);
      }
    } catch (err) {
      addLog(`API Request Failed (${endpoint}): ${err}`)
    }
  }

  const handlePlaceOrder = () => {
    handleJsonApiRequest('place_order', {
      symbol: symbol,
      side: manualSide,
      order_type: manualType,
      quantity: manualQty,
      price: manualPrice,
      position_side: manualPosSide
    })
  }

  const handleCancelOrder = (orderId: string) => {
    if (!orderId) { addLog('No order ID selected'); return; }
    handleJsonApiRequest('cancel_order', { symbol, order_id: orderId })
    setSelectedOrderId("")
  }

  const handleCancelAllOrders = () => {
    handleJsonApiRequest('cancel_all_orders', { symbol })
  }

  const handleClosePositionMarket = (posSide: string) => {
    handleJsonApiRequest('close_position_market', { symbol, position_side: posSide })
  }

  const handleApplyTpRules = () => {
    const levels = tpLevels
      .filter(l => l.pnl && l.close)
      .map(l => ({ pnl_threshold_percent: l.pnl, close_percent_of_pos: l.close }))
    handleJsonApiRequest('apply_tp_rules', { levels })
  }

  const addTpLevel = () => {
    if (tpLevels.length < 5) {
      setTpLevels([...tpLevels, {pnl: "", close: ""}])
    }
  }

  const updateTpLevel = (idx: number, field: 'pnl' | 'close', val: string) => {
    const copy = [...tpLevels];
    copy[idx] = {...copy[idx], [field]: val};
    setTpLevels(copy);
  }

  useEffect(() => {
    const connectWs = () => {
      const ws = new WebSocket('ws://localhost:8000/ws/stream')
      
      ws.onopen = () => {
        setConnected(true)
        addLog("Connected to server via WebSocket")
        wsRef.current = ws;
      }
      
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          if (data.type === 'market_update') {
            if (data.price) setMarketPrice(data.price);
            if (data.symbol) setSymbol(data.symbol);
            if (data.available_symbols) setAvailableSymbols(data.available_symbols);
            if (data.long_grid) setLongGrid(data.long_grid);
            if (data.short_grid) setShortGrid(data.short_grid);
            if (data.ready_tfs) setReadyTfs(data.ready_tfs);
            if (data.precisions) setAssetPrecisions(data.precisions);
            if (data.bot_config) {
              setLeverage(data.bot_config.leverage);
              setInitialMargin(data.bot_config.initial_margin);
              setMarginMultiplier(data.bot_config.margin_multiplier);
              setClusterCount(data.bot_config.cluster_count);
              setCapitalLimit(data.bot_config.capital_limit);
              setMinStake(data.bot_config.min_stake);
            }
          } else if (data.type === 'gui_event') {
             if (data.action === "live_log" || data.action === "error_log" || data.action === "info_message") {
                 addLog(typeof data.data === 'string' ? data.data : JSON.stringify(data.data));
             } else if (data.action === "update_connection_status") {
                 addLog(`[Status]: ${data.data.message || ''}`);
                 setBinanceStatus(data.data.connected ? "Connected" : "Disconnected");
             } else if (data.action !== "update_open_orders_display" && data.action !== "update_positions_display" && data.action !== "update_trade_history_display" && data.action !== "update_live_managed_grid_orders_display" && data.action !== "symbol_data_status") {
                 addLog(`[GUI Event]: ${data.action}`);
             }
             
             if (data.action === "live_trading_status_update_button") {
                 setLiveTradingStatus(data.data.active ? "Running" : "Stopped")
             }
             if (data.action === "update_open_orders_display") {
                 setOpenOrders(data.data || [])
             }
             if (data.action === "update_positions_display" && data.data.positions) {
                 const posData = data.data.positions;
                 setPositions(posData);
                 let lPnl = 0; let sPnl = 0;
                 let lMargin = 0; let sMargin = 0;
                 const userLev = parseFloat(leverage) || 1;
                 posData.forEach((p: any) => {
                    const amt = parseFloat(p.amount);
                    const pnl = parseFloat(p.pnl_usd);
                    const entry = parseFloat(p.entry_price);
                    const margin = Math.abs(amt) * entry / userLev;
                    if (p.position_side === 'LONG' || amt > 0) { lPnl += pnl; lMargin += margin; }
                    if (p.position_side === 'SHORT' || amt < 0) { sPnl += pnl; sMargin += margin; }
                 });
                 setLongPnlUsd(lPnl);
                 setShortPnlUsd(sPnl);
                 setLongPnlPercent(lMargin > 0 ? (lPnl / lMargin) * 100 : 0);
                 setShortPnlPercent(sMargin > 0 ? (sPnl / sMargin) * 100 : 0);
             }
             if (data.action === "update_trade_history_display") {
                 setTradeHistory(data.data || []);
             }
             if (data.action === "update_live_managed_grid_orders_display") {
                 setManagedOrders(data.data || [])
             }
          }
        } catch (e) {
          console.error("Failed to parse message", e)
        }
      }
      
      ws.onclose = () => {
        setConnected(false)
        setTimeout(connectWs, 3000)
      }
      
      ws.onerror = () => {
        setConnected(false)
      }

      wsRef.current = ws;
    }

    connectWs()

    return () => {
      if (wsRef.current) wsRef.current.close()
    }
  }, [])

  return (
    <div className="app-container">
      <header className="header" style={{ flexDirection: 'column', alignItems: 'flex-start', paddingBottom: '1rem', marginBottom: '1rem' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%', marginBottom: '1rem' }}>
          <h1>BotV3 Dashboard</h1>
          <div style={{display: 'flex', gap: '2rem', alignItems: 'center'}}>
            <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
              <span style={{ fontSize: '0.9rem', color: 'var(--text-muted)', fontWeight: 600 }}>Strategy:</span>
              <select 
                value={strategyProfile} 
                onChange={e => handleSetProfile(e.target.value)}
                style={{ padding: '0.4rem 0.8rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--accent-blue)', color: 'white', fontSize: '0.9rem', fontWeight: 600, cursor: 'pointer' }}
              >
                <option value="conservative">🛡️ Conservative</option>
                <option value="moderate_v2">⚖️ Moderate V2</option>
                <option value="aggressive_v2">🔥 Aggressive V2</option>
              </select>
            </div>
            <div style={{fontSize: '1.25rem', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '0.5rem'}}>
              <select 
                 value={symbol} 
                 onChange={e => {
                    setSymbol(e.target.value);
                    if (wsRef.current) wsRef.current.send(JSON.stringify({type: "change_symbol", symbol: e.target.value}));
                 }}
                 style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', fontSize: '1.1rem', fontWeight: 600 }}
              >
                {availableSymbols.map(sym => (
                    <option key={sym} value={sym}>{sym}</option>
                ))}
              </select>
              <span style={{ color: 'var(--text-main)' }}>${marketPrice.toFixed(getPriceDecimals(symbol, assetPrecisions))}</span>
            </div>
            <div className="status-badge">
              <div className={`status-dot ${connected ? 'connected' : ''}`}></div>
              API: {connected ? 'Connected' : 'Offline'}
            </div>
          </div>
        </div>

        {/* ПАНЕЛЬ СТАТУСУ ТА АНАЛІЗУ */}
        <div style={{ display: 'flex', gap: '1rem', width: '100%', marginBottom: '1rem', background: 'var(--bg-panel)', padding: '0.75rem 1rem', borderRadius: '12px', border: '1px solid var(--border-color)', alignItems: 'center', justifyContent: 'space-between' }}>
           <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
              <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>Analysis Ready:</span>
              <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                {readyTfs.length > 0 ? readyTfs.map(tf => (
                  <span key={tf} className="count-badge" style={{ background: 'rgba(52, 211, 153, 0.2)', color: '#34d399', border: '1px solid rgba(52, 211, 153, 0.3)' }}>{tf}</span>
                )) : <span style={{ color: '#fbbf24', fontSize: '0.8rem' }}>Loading market data...</span>}
              </div>
           </div>
           <div className="status-badge">
             <div className={`status-dot ${connected ? 'connected' : ''}`}></div>
             API: {connected ? 'Connected' : 'Offline'}
           </div>
        </div>

        {/* ПАНЕЛЬ УПРАВЛІННЯ LIVE ТОРГІВЛЕЮ */}
        <div style={{ display: 'flex', gap: '1rem', width: '100%', marginBottom: '1rem' }}>
          
          <div style={{ background: 'var(--bg-panel)', padding: '1rem', borderRadius: '12px', border: '1px solid var(--border-color)', display: 'flex', flexDirection: 'column', gap: '1rem' }}>
            {binanceStatus !== 'Connected' && (
              <div style={{ display: 'flex', gap: '10px' }}>
                <input 
                  type="password" 
                  placeholder="Binance API Key (обов'язково при першому запуску)" 
                  value={inputApiKey} 
                  onChange={(e) => setInputApiKey(e.target.value)} 
                  style={{ flex: 1, padding: '0.5rem', borderRadius: '6px', border: '1px solid #374151', background: '#1f2937', color: 'white' }} 
                />
                <input 
                  type="password" 
                  placeholder="Binance API Secret" 
                  value={inputApiSecret} 
                  onChange={(e) => setInputApiSecret(e.target.value)} 
                  style={{ flex: 1, padding: '0.5rem', borderRadius: '6px', border: '1px solid #374151', background: '#1f2937', color: 'white' }} 
                />
              </div>
            )}
            <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
            <button 
              onClick={() => {
                if (binanceStatus === 'Connected') {
                  handleApiRequest('disconnect')
                } else {
                  handleJsonApiRequest('connect', { api_key: inputApiKey, api_secret: inputApiSecret })
                }
              }} 
              disabled={binanceStatus === 'Connecting...'}
              style={{ 
                  padding: '0.5rem 1rem', 
                  borderRadius: '6px', 
                  background: binanceStatus === 'Connected' ? 'var(--accent-red)' : (binanceStatus === 'Connecting...' ? '#6b7280' : 'var(--accent-green)'), 
                  color: 'white', 
                  border: 'none', 
                  cursor: binanceStatus === 'Connecting...' ? 'not-allowed' : 'pointer',
                  fontWeight: 'bold',
                  minWidth: '200px'
              }}>
              {binanceStatus === 'Connected' ? '🔗 Disconnect from Binance' : (binanceStatus === 'Connecting...' ? 'Connecting...' : '🔗 Connect to Binance')}
            </button>
            <button 
              onClick={() => handleJsonApiRequest('calculate_grid', {})}
              style={{ padding: '0.5rem 1rem', borderRadius: '6px', background: '#8b5cf6', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 'bold' }}>
              📊 Calculate Grid
            </button>
            <button 
              onClick={() => handleJsonApiRequest('clear_grids', {})}
              style={{ padding: '0.5rem 1rem', borderRadius: '6px', background: '#4b5563', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 'bold' }}>
              🗑️ Clear Grids
            </button>
            </div>
            <button 
              onClick={() => handleApiRequest(liveTradingStatus === 'Running' ? 'stop_trading' : 'start_trading')} 
              disabled={liveTradingStatus !== 'Running' && longGrid.length === 0 && shortGrid.length === 0}
              style={{ 
                padding: '0.5rem 1rem', 
                borderRadius: '6px', 
                background: liveTradingStatus === 'Running' ? 'var(--accent-red)' : ((longGrid.length > 0 || shortGrid.length > 0) ? 'var(--accent-green)' : '#4b5563'), 
                color: 'white', 
                border: 'none', 
                cursor: (liveTradingStatus === 'Running' || longGrid.length > 0 || shortGrid.length > 0) ? 'pointer' : 'not-allowed', 
                fontWeight: 'bold',
                minWidth: '150px'
              }}>
              {liveTradingStatus === 'Running' ? '🛑 STOP BOT' : `🚀 START BOT (${longGrid.length + shortGrid.length} proj.)`}
            </button>
            <button 
              onClick={() => { if(window.confirm("Zупинити ВЕСЬ сервер?")) handleApiRequest('shutdown') }} 
              style={{ padding: '0.5rem 1rem', borderRadius: '6px', background: '#333', color: '#ff4444', border: '1px solid #ff4444', cursor: 'pointer', fontWeight: 'bold', fontSize: '0.8rem' }}>
              🔌 Shutdown Server
            </button>
            <div style={{ color: liveTradingStatus === 'Running' ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 'bold' }}>
              Status: {liveTradingStatus}
            </div>
          </div>

          <div style={{ background: 'var(--bg-panel)', padding: '1rem', borderRadius: '12px', border: '1px solid var(--border-color)', display: 'flex', gap: '2rem', flex: 1, alignItems: 'center' }}>
             <div>
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>Long Position PNL</div>
                <div style={{ fontSize: '1.25rem', color: longPnlUsd >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 'bold' }}>
                  {longPnlUsd >= 0 ? '+' : ''}{longPnlUsd.toFixed(2)} USD
                </div>
                <div style={{ fontSize: '0.9rem', color: longPnlPercent >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                  {longPnlPercent >= 0 ? '+' : ''}{longPnlPercent.toFixed(2)}%
                </div>
             </div>
             <div>
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>Short Position PNL</div>
                <div style={{ fontSize: '1.25rem', color: shortPnlUsd >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 'bold' }}>
                  {shortPnlUsd >= 0 ? '+' : ''}{shortPnlUsd.toFixed(2)} USD
                </div>
                <div style={{ fontSize: '0.9rem', color: shortPnlPercent >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                  {shortPnlPercent >= 0 ? '+' : ''}{shortPnlPercent.toFixed(2)}%
                </div>
             </div>
          </div>

        </div>
        
        {/* Панель налаштувань сітки */}
        <div style={{ display: 'flex', gap: '1rem', background: 'var(--bg-panel)', padding: '1rem', borderRadius: '12px', border: '1px solid var(--border-color)', width: '100%' }}>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Leverage (x)</label>
            <input type="number" value={leverage} onChange={e => setLeverage(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Initial Margin ($)</label>
            <input type="number" value={initialMargin} onChange={e => setInitialMargin(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Margin Multiplier</label>
            <input type="number" step="0.1" value={marginMultiplier} onChange={e => setMarginMultiplier(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Cluster Count</label>
            <input type="number" value={clusterCount} onChange={e => setClusterCount(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', width: '80px' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Capital Limit ($)</label>
            <input type="number" value={capitalLimit} onChange={e => setCapitalLimit(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', width: '100px' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Min Order ($)</label>
            <input type="number" step="0.5" value={minStake} onChange={e => setMinStake(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', width: '100px' }} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>Recalc Every (h)</label>
            <input type="number" value={recalcInterval} onChange={e => setRecalcInterval(e.target.value)} style={{ padding: '0.5rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', width: '80px' }} />
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <button onClick={sendConfigToServer} style={{ flex: 1, padding: '0.8rem', borderRadius: '6px', background: 'var(--primary-color)', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 'bold' }}>
              Save Settings
            </button>
          </div>
        </div>
      </header>

      <main className="dashboard-grid">
        {/* --- Позиції та Історія --- */}
        <div style={{ gridColumn: 'span 2', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem' }}>
           <LivePositionsTable positions={positions} userLeverage={parseFloat(leverage) || 1} assetPrecisions={assetPrecisions} />
           <TradeHistoryTable tradeHistory={tradeHistory} assetPrecisions={assetPrecisions} />
        </div>

        {/* --- Bot Grid projection is shown at the bottom for both Long and Short --- */}

        {/* --- Open Orders + Cancel --- */}
        <div style={{ gridColumn: 'span 2' }}>
          <div className="panel" style={{ overflowY: 'auto', maxHeight: '350px' }}>
            <div className="panel-header">
              <span style={{color: 'white', fontWeight: 600}}>Live Open Orders (Binance)</span>
              <span className="count-badge">{openOrders.length} active</span>
            </div>
            {openOrders.length > 0 ? (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>ID</th><th>Side/Type</th><th>Pos Side</th><th>Price</th><th>Amount</th><th>Status</th><th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {openOrders.map((o) => (
                    <tr key={o.orderId} onClick={() => setSelectedOrderId(String(o.orderId))} style={{ cursor: 'pointer', background: selectedOrderId === String(o.orderId) ? 'rgba(99,102,241,0.3)' : 'transparent' }}>
                      <td style={{fontSize: '0.75rem'}}>{String(o.orderId).slice(-6)}</td>
                      <td><span style={{color: o.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)'}}>{o.side}</span> {o.type}</td>
                      <td>{o.positionSide}</td>
                      <td>{parseFloat(o.price).toFixed(getPriceDecimals(o.symbol, assetPrecisions))}</td>
                      <td>{o.origQty}</td>
                      <td>{o.status}</td>
                      <td>
                        <button onClick={(e) => { e.stopPropagation(); handleCancelOrder(String(o.orderId)); }}
                          style={{ padding: '2px 8px', borderRadius: '4px', background: 'var(--accent-red)', color: 'white', border: 'none', cursor: 'pointer', fontSize: '0.75rem' }}>
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="empty-state">No Live Orders Open on Binance</div>
            )}
            <div style={{ display: 'flex', gap: '0.5rem', padding: '0.5rem', borderTop: '1px solid var(--border-color)' }}>
              <input value={selectedOrderId} onChange={e => setSelectedOrderId(e.target.value)} placeholder="Order ID" 
                style={{ flex: 1, padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', fontSize: '0.85rem' }} />
              <button onClick={() => handleCancelOrder(selectedOrderId)}
                style={{ padding: '0.4rem 0.8rem', borderRadius: '6px', background: '#f59e0b', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: '0.85rem' }}>
                Cancel Selected
              </button>
              <button onClick={handleCancelAllOrders}
                style={{ padding: '0.4rem 0.8rem', borderRadius: '6px', background: 'var(--accent-red)', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: '0.85rem' }}>
                Cancel All ({symbol})
              </button>
            </div>
          </div>
        </div>

        {/* --- Manual Order + TP Levels (side by side) --- */}
        <div style={{ gridColumn: 'span 2', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem' }}>
          {/* --- Manual Order Panel --- */}
          <div className="panel" style={{ padding: '1rem' }}>
            <div className="panel-header"><span style={{color: 'white', fontWeight: 600}}>Ручний Реальний Ордер</span></div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '0.5rem', marginTop: '0.5rem' }}>
              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Side</label>
                <select value={manualSide} onChange={e => setManualSide(e.target.value)}
                  style={{ width: '100%', padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: manualSide === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
                  <option value="BUY">BUY</option><option value="SELL">SELL</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Type</label>
                <select value={manualType} onChange={e => setManualType(e.target.value)}
                  style={{ width: '100%', padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }}>
                  <option value="LIMIT">LIMIT</option><option value="MARKET">MARKET</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Position Side</label>
                <select value={manualPosSide} onChange={e => setManualPosSide(e.target.value)}
                  style={{ width: '100%', padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }}>
                  <option value="LONG">LONG</option><option value="SHORT">SHORT</option>
                </select>
              </div>
              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Quantity</label>
                <input value={manualQty} onChange={e => setManualQty(e.target.value)} placeholder="0.001"
                  style={{ width: '100%', padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }} />
              </div>
              {manualType === 'LIMIT' && (
                <div style={{ gridColumn: 'span 2' }}>
                  <label style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Price (LIMIT)</label>
                  <input value={manualPrice} onChange={e => setManualPrice(e.target.value)} placeholder="0.00"
                    style={{ width: '100%', padding: '0.4rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white' }} />
                </div>
              )}
            </div>
            <button onClick={handlePlaceOrder}
              style={{ width: '100%', marginTop: '0.75rem', padding: '0.5rem', borderRadius: '6px', background: 'var(--accent-blue)', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 'bold' }}>
              📝 Place Order
            </button>
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
              <button onClick={() => handleClosePositionMarket('LONG')}
                style={{ flex: 1, padding: '0.4rem', borderRadius: '6px', background: '#f97316', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem' }}>
                Close LONG (Market)
              </button>
              <button onClick={() => handleClosePositionMarket('SHORT')}
                style={{ flex: 1, padding: '0.4rem', borderRadius: '6px', background: '#ef4444', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 600, fontSize: '0.8rem' }}>
                Close SHORT (Market)
              </button>
            </div>
          </div>

          {/* --- Take Profit Levels Panel --- */}
          <div className="panel" style={{ padding: '1rem' }}>
            <div className="panel-header">
              <span style={{color: 'white', fontWeight: 600}}>Partial Take-Profit (PNL)</span>
              <span style={{ fontSize: '0.75rem', color: '#fbbf24', marginLeft: 'auto' }}>Range: 20% - 150%</span>
            </div>
            <div style={{ marginTop: '0.5rem' }}>
              {tpLevels.map((level, i) => (
                <div key={i} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.4rem' }}>
                  <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)', minWidth: '35px' }}>Lv.{i+1}</span>
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>PNL% &gt;</span>
                  <input value={level.pnl} onChange={e => updateTpLevel(i, 'pnl', e.target.value)} placeholder="20.0"
                    style={{ width: '60px', padding: '0.3rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', textAlign: 'center' }} />
                  <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Close% =</span>
                  <input value={level.close} onChange={e => updateTpLevel(i, 'close', e.target.value)} placeholder="25"
                    style={{ width: '60px', padding: '0.3rem', borderRadius: '6px', background: 'var(--bg-dark)', border: '1px solid var(--border-color)', color: 'white', textAlign: 'center' }} />
                </div>
              ))}
            </div>
            <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
              <button onClick={addTpLevel} disabled={tpLevels.length >= 5}
                style={{ flex: 1, padding: '0.4rem', borderRadius: '6px', background: tpLevels.length >= 5 ? '#4b5563' : '#6366f1', color: 'white', border: 'none', cursor: tpLevels.length >= 5 ? 'not-allowed' : 'pointer', fontSize: '0.85rem' }}>
                + Add Level ({tpLevels.length}/5)
              </button>
              <button onClick={handleApplyTpRules}
                style={{ flex: 1, padding: '0.4rem', borderRadius: '6px', background: 'var(--accent-green)', color: 'white', border: 'none', cursor: 'pointer', fontWeight: 'bold', fontSize: '0.85rem' }}>
                ✓ Save rules
              </button>
            </div>
          </div>
        </div>

        {/* --- Grid Projections --- */}
        <div style={{ gridColumn: 'span 2', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1.5rem' }}>
          <OrderTable title="Long Grid Projection" orders={longGrid} type="long" symbol={symbol} assetPrecisions={assetPrecisions} />
          <OrderTable title="Short Grid Projection" orders={shortGrid} type="short" symbol={symbol} assetPrecisions={assetPrecisions} />
        </div>

        {/* --- Managed Orders (Active Bot Logic) --- */}
        <div style={{ gridColumn: 'span 2' }}>
            <ManagedOrdersTable orders={managedOrders} symbol={symbol} assetPrecisions={assetPrecisions} />
        </div>
      </main>

      <div className="log-container" style={{ marginTop: '1rem', height: '150px' }}>
        {logs.map((log, i) => (
          <div key={i} className="log-entry">
            <span className="log-time">[{log.time}]</span>
            <span className="log-message">{log.message}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default App
