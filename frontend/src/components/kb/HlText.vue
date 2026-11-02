<template>
  <span><template v-for="(s, i) in segs" :key="i"><mark v-if="s.hl">{{ s.text }}</mark><template v-else>{{ s.text }}</template></template></span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { highlight } from '@/utils/text'

const props = defineProps<{ text: string; terms?: string[] }>()
const segs = computed(() => highlight(props.text, props.terms ?? []))
</script>

<style scoped>
mark { background: rgba(245, 165, 36, 0.28); color: #ffd58a; border-radius: 2px; padding: 0 1px; }
</style>
