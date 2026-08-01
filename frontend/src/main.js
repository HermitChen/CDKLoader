import { createApp } from 'vue'
import { nanocatZhCN, setNanocatLocale } from 'nanocat-ui'
import 'nanocat-ui/styles.css'
import App from './App.vue'
import './style.css'

setNanocatLocale(nanocatZhCN)

createApp(App).mount('#app')
